import openai
import sys
import os
import json
import traceback
import re
import uuid
import requests
from datetime import datetime
from flask import Flask, request, abort, render_template, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
from firebase_admin import firestore, storage
import firebase_admin
from firebase_admin import credentials, firestore
import base64

sys.stdout.reconfigure(encoding='utf-8')
#測試是否有git
app = Flask(__name__)
print("✅ Flask App initialized")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")
IMGUR_CLIENT_ID = os.environ.get("IMGUR_CLIENT_ID")
IMGUR_CLIENT_SECRET = os.environ.get("IMGUR_CLIENT_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

def get_firebase_credentials_from_env():
    return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))

firebase_admin.initialize_app(get_firebase_credentials_from_env())
db = firestore.client()

user_sessions = {}
user_message_counts = {}
story_summaries = {}
story_titles = {}
story_image_prompts = {}
story_image_urls = {}
story_current_paragraph = {}

@app.route("/")
def index():
    return "LINE GPT Webhook is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

def reset_story_memory(user_id):
    """重置使用者的故事相關記憶"""
    if user_id in user_sessions:
        user_sessions[user_id] = {"messages": []}
    if user_id in user_message_counts:
        user_message_counts[user_id] = 0
    if user_id in story_summaries:
        story_summaries[user_id] = ""
    if user_id in story_titles:
        story_titles[user_id] = ""
    if user_id in story_image_prompts:
        story_image_prompts[user_id] = ""
    if user_id in story_image_urls:
        story_image_urls[user_id] = {}
    if user_id in story_current_paragraph:
        story_current_paragraph[user_id] = 0
    print(f"✅ 已重置使用者 {user_id} 的故事記憶")

def generate_story_summary(messages):
    """根據對話歷史生成故事總結"""
    try:
        summary_prompt = """
請將以下對話內容整理成五個段落的故事情節，每個段落用數字標記（1. 2. 3. 4. 5.）。
請遵循以下格式要求：
1. 每個段落必須單獨一行
2. 每個段落約20字左右
3. 保持故事的連貫性
4. 使用簡潔的文字描述
5. 確保每個段落都清楚表達故事的重要情節

範例格式：
1. 小明在森林裡發現一隻受傷的小鳥。
2. 他決定帶小鳥回家照顧。
3. 經過細心照料，小鳥逐漸康復。
4. 小鳥學會了飛行，但捨不得離開。
5. 最後小鳥選擇留下來陪伴小明。

請按照以上格式整理故事內容。
"""
        messages_for_summary = [
            {"role": "system", "content": summary_prompt},
            {"role": "user", "content": "以下是故事對話內容："},
            *messages
        ]
        
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages_for_summary,
            temperature=0.7,
        )
        return response.choices[0].message["content"]
    except Exception as e:
        print("❌ 生成故事總結失敗：", e)
        return None

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    reply_token = event.reply_token
    print(f"📩 收到使用者 {user_id} 的訊息：{user_text}")

    try:
        # 檢查是否包含「開始說故事」的關鍵字
        if re.search(r"(開始說故事|說個故事|講個故事|說一個故事|講一個故事)", user_text):
            reset_story_memory(user_id)
            line_bot_api.reply_message(reply_token, TextSendMessage(text="好的，讓我們開始創作一個新的故事吧！請告訴我你想要創作什麼樣的故事呢？"))
            return

        # 檢查是否要求總結故事
        if re.search(r"(幫我統整|整理|總結|歸納|目前的故事)", user_text):
            if user_id not in user_sessions or not user_sessions[user_id]["messages"]:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="目前還沒有故事內容可以總結喔！"))
                return
                
            summary = generate_story_summary(user_sessions[user_id]["messages"])
            if summary:
                # 在總結後加入插圖階段的提議
                formatted_summary = "以下是目前的故事內容：\n\n" + summary + "\n\n故事已經完成了！要不要開始為故事畫插圖呢？我們可以從第一段故事開始，請告訴我你想要如何描繪第一段故事的場景？"
                line_bot_api.reply_message(reply_token, TextSendMessage(text=formatted_summary))
                save_to_firebase(user_id, "user", user_text)
                save_to_firebase(user_id, "assistant", formatted_summary)
                # 重置當前段落為第一段
                story_current_paragraph[user_id] = 0
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，我現在無法總結故事，請稍後再試。"))
            return

        match = re.search(r"(?:請畫|幫我畫|生成.*圖片|畫.*圖|我想要一張.*圖)(.*)", user_text)
        if match:
            prompt = match.group(1).strip()
            current_paragraph = story_current_paragraph.get(user_id, 0) + 1
            image_url = generate_dalle_image(prompt, user_id)
            if image_url:
                # 在回覆中加入當前段落資訊
                reply_text = f"這是第 {current_paragraph} 段故事的插圖："
                line_bot_api.reply_message(reply_token, [
                    TextSendMessage(text=reply_text),
                    ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
                ])
                save_to_firebase(user_id, "user", user_text)
                save_to_firebase(user_id, "assistant", f"{reply_text}\n{image_url}")
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="小頁畫不出這張圖，試試其他描述看看 🖍️"))
            return

        assistant_reply = get_openai_response(user_id, user_text)
        if not assistant_reply:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="小頁暫時卡住了，請稍後再試 🌧️"))
            return

        line_bot_api.reply_message(reply_token, TextSendMessage(text=assistant_reply))
        save_to_firebase(user_id, "user", user_text)
        save_to_firebase(user_id, "assistant", assistant_reply)

    except Exception as e:
        print("❌ 發生錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="小頁出了一點小狀況，請稍後再試 🙇"))

def save_to_firebase(user_id, role, text):
    try:
        user_doc_ref = db.collection("users").document(user_id)
        user_doc_ref.collection("chat").add({
            "role": role,
            "text": text,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
        print(f"✅ Firebase 已儲存訊息（{role}）")
    except Exception as e:
        print(f"⚠️ 儲存 Firebase 失敗（{role}）：", e)

base_system_prompt = """
你是「小頁」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助一位 50 歲以上的長輩創作 5 段故事繪本。
請用簡潔、好讀的語氣回應，每則訊息盡量不超過 35 字並適當分段。
第一階段：故事創作引導，引導使用者想像角色、場景與情節，發展成五段故事。
不要主導故事，保持引導與陪伴。
第二階段：插圖引導，幫助使用者描述畫面，並在完成後詢問是否需調整。
請自稱「小頁」，以朋友般的語氣陪伴使用者完成創作。
""".strip()

def format_reply(text):
    return re.sub(r'([。！？])\s*', r'\1\n', text)

def get_openai_response(user_id, user_message):
    if user_id not in user_sessions:
        user_sessions[user_id] = {"messages": []}
    if user_id not in user_message_counts:
        user_message_counts[user_id] = 0
    if user_id not in story_summaries:
        story_summaries[user_id] = ""
    if user_id not in story_current_paragraph:
        story_current_paragraph[user_id] = 0

    user_sessions[user_id]["messages"].append({"role": "user", "content": user_message})
    user_message_counts[user_id] += 1

    # 更新當前段落
    if user_message_counts[user_id] % 6 == 0:  # 每6條訊息更新一次段落
        story_current_paragraph[user_id] = min(4, story_current_paragraph[user_id] + 1)

    if user_message_counts[user_id] == 30:
        user_sessions[user_id]["messages"].append({
            "role": "user",
            "content": "請為這三十段故事取個標題，大約五六個字就好。"
        })

    summary_context = story_summaries[user_id]
    prompt_with_summary = base_system_prompt
    if summary_context:
        prompt_with_summary += f"\n\n【故事摘要】\n{summary_context}\n請根據以上摘要，延續創作對話內容。"

    recent_history = user_sessions[user_id]["messages"][-30:]
    messages = [{"role": "system", "content": prompt_with_summary}] + recent_history

    try:
        print(f"📦 傳給 OpenAI 的訊息：{json.dumps(messages, ensure_ascii=False)}")
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
        )
        assistant_reply = response.choices[0].message["content"]
        assistant_reply = format_reply(assistant_reply)

        user_sessions[user_id]["messages"].append({"role": "assistant", "content": assistant_reply})

        if user_message_counts[user_id] == 30:
            summary = extract_summary_from_reply(assistant_reply)
            title = extract_title_from_reply(assistant_reply)
            story_summaries[user_id] = summary
            story_titles[user_id] = title
            # 準備生成封面
            prompt = f"故事名稱：{title}，主題是：{summary}"
            story_image_prompts[user_id] = prompt

        return assistant_reply

    except Exception as e:
        print("❌ OpenAI 回應錯誤：", e)
        traceback.print_exc()
        return None

def extract_summary_from_reply(reply_text):
    parts = reply_text.strip().split("\n")
    for part in reversed(parts):
        if "這段故事" in part or "總結" in part or "目前的故事內容" in part:
            return part.strip()
    return ""

def extract_title_from_reply(reply_text):
    match = re.search(r"(?:故事名稱|標題)[:：]?([\w\u4e00-\u9fff]{3,8})", reply_text)
    return match.group(1).strip() if match else "我們的故事"

def generate_dalle_image(prompt, user_id):
    try:
        # 檢查是否已經生成過圖片
        if user_id in story_image_urls and prompt in story_image_urls[user_id]:
            return story_image_urls[user_id][prompt]  # 返回已經儲存的圖片

        # 如果沒有生成過圖片，則生成新圖片
        print(f"🖍️ 產生圖片中：{prompt}")
        response = openai.Image.create(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            response_format="url"
        )
        image_url = response['data'][0]['url']
        print(f"✅ 產生圖片成功：{image_url}")
        
        # 儲存圖片 URL
        if user_id not in story_image_urls:
            story_image_urls[user_id] = {}
        story_image_urls[user_id][prompt] = image_url  # 儲存每個用戶的圖片 URL 和 prompt
        
        # 下載並上傳到 Imgur
        try:
            # 下載圖片
            print("⬇️ 開始下載圖片...")
            img_data = requests.get(image_url).content
            print("✅ 圖片下載完成")
            
            # 上傳到 Imgur
            print("💾 開始上傳到 Imgur...")
            # 將圖片轉換為 base64
            img_base64 = base64.b64encode(img_data).decode('utf-8')
            
            # 準備上傳資料
            url = "https://api.imgur.com/3/image"
            headers = {
                "Authorization": f"Client-ID {IMGUR_CLIENT_ID}"
            }
            data = {
                "image": img_base64,
                "type": "base64",
                "privacy": "hidden"  # 設定為私有
            }
            
            # 上傳圖片
            response = requests.post(url, headers=headers, data=data)
            response_data = response.json()
            
            if response.status_code == 200 and response_data['success']:
                imgur_url = response_data['data']['link']
                deletehash = response_data['data']['deletehash']  # 儲存刪除雜湊值
                print(f"✅ 圖片已上傳到 Imgur：{imgur_url}")
                
                # 儲存圖片 URL 和刪除雜湊值到 Firestore
                user_doc_ref = db.collection("users").document(user_id)
                user_doc_ref.collection("images").add({
                    "url": imgur_url,
                    "deletehash": deletehash,  # 儲存刪除雜湊值
                    "prompt": prompt,
                    "timestamp": firestore.SERVER_TIMESTAMP
                })
                print("✅ 圖片資訊已儲存到 Firestore")
                
                return imgur_url
            else:
                print(f"❌ Imgur API 回應錯誤：{response_data}")
                return image_url  # 如果 Imgur 上傳失敗，返回原始 URL
            
        except Exception as e:
            print(f"❌ 上傳圖片到 Imgur 失敗：{e}")
            traceback.print_exc()
            return image_url  # 如果 Imgur 上傳失敗，返回原始 URL
        
    except Exception as e:
        print("❌ 產生圖片失敗：", e)
        traceback.print_exc()
        return None

@app.route("/story/<user_id>")
def view_story(user_id):
    try:
        # 從 Firebase 獲取使用者資料
        user_doc_ref = db.collection("users").document(user_id)
        images = user_doc_ref.collection("images").order_by("timestamp").get()
        chat = user_doc_ref.collection("chat").order_by("timestamp").get()
        
        # 整理資料
        story_data = {
            "title": story_titles.get(user_id, "我們的故事"),
            "summary": story_summaries.get(user_id, ""),
            "images": [],
            "content": []
        }
        
        # 處理圖片
        for img in images:
            story_data["images"].append({
                "url": img.get("url"),
                "prompt": img.get("prompt")
            })
            
        # 處理對話內容
        for msg in chat:
            if msg.get("role") == "assistant":
                story_data["content"].append(msg.get("text"))
        
        return render_template("story.html", story=story_data)
    except Exception as e:
        print(f"❌ 讀取故事失敗：{e}")
        return "無法讀取故事", 404

@app.route("/api/story/<user_id>")
def get_story_data(user_id):
    try:
        # 從 Firebase 獲取使用者資料
        user_doc_ref = db.collection("users").document(user_id)
        images = user_doc_ref.collection("images").order_by("timestamp").get()
        chat = user_doc_ref.collection("chat").order_by("timestamp").get()
        
        # 整理資料
        story_data = {
            "title": story_titles.get(user_id, "我們的故事"),
            "summary": story_summaries.get(user_id, ""),
            "images": [],
            "content": []
        }
        
        # 處理圖片
        for img in images:
            story_data["images"].append({
                "url": img.get("url"),
                "prompt": img.get("prompt")
            })
            
        # 處理對話內容
        for msg in chat:
            if msg.get("role") == "assistant":
                story_data["content"].append(msg.get("text"))
        
        return jsonify(story_data)
    except Exception as e:
        print(f"❌ 讀取故事失敗：{e}")
        return jsonify({"error": "無法讀取故事"}), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
