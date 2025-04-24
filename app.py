import openai
import sys
import os
import json
import traceback
import re
import uuid
import requests
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
from firebase_admin import firestore, storage
import firebase_admin
from firebase_admin import credentials, firestore

sys.stdout.reconfigure(encoding='utf-8')
#測試是否有git
app = Flask(__name__)
print("✅ Flask App initialized")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    reply_token = event.reply_token
    print(f"📩 收到使用者 {user_id} 的訊息：{user_text}")

    try:
        match = re.search(r"(?:請畫|幫我畫|生成.*圖片|畫.*圖|我想要一張.*圖)(.*)", user_text)
        if match:
            prompt = match.group(1).strip()
            image_url = generate_dalle_image(prompt, user_id)
            if image_url:
                line_bot_api.reply_message(reply_token, ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
                save_to_firebase(user_id, "user", user_text)
                save_to_firebase(user_id, "assistant", image_url)
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
第二階段：插圖引導，插圖風格溫馨童趣、色彩柔和、畫面簡單。
幫助使用者描述畫面，並在完成後詢問是否需調整。
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

    user_sessions[user_id]["messages"].append({"role": "user", "content": user_message})
    user_message_counts[user_id] += 1

    if user_message_counts[user_id] == 5:
        user_sessions[user_id]["messages"].append({
            "role": "user",
            "content": "請為這五段故事取個標題，大約五六個字就好。"
        })

    summary_context = story_summaries[user_id]
    prompt_with_summary = base_system_prompt
    if summary_context:
        prompt_with_summary += f"\n\n【故事摘要】\n{summary_context}\n請根據以上摘要，延續創作對話內容。"

    recent_history = user_sessions[user_id]["messages"][-5:]
    messages = [{"role": "system", "content": prompt_with_summary}] + recent_history

    try:
        print(f"📦 傳給 OpenAI 的訊息：{json.dumps(messages, ensure_ascii=False)}")
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.7,
        )
        assistant_reply = response.choices[0].message["content"]
        assistant_reply = format_reply(assistant_reply)

        user_sessions[user_id]["messages"].append({"role": "assistant", "content": assistant_reply})

        if user_message_counts[user_id] == 5:
            summary = extract_summary_from_reply(assistant_reply)
            title = extract_title_from_reply(assistant_reply)
            story_summaries[user_id] = summary
            story_titles[user_id] = title
            # 準備生成封面
            prompt = f"故事名稱：{title}，主題是：{summary}，畫風為溫馨童趣、色彩柔和、畫面簡單"
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
        full_prompt = f"{prompt}。請用繪本風格：乾淨、清爽、溫馨。畫風一致。"
        print(f"🖼️ 產生圖片中：{full_prompt}")
        response = openai.Image.create(
            model="dall-e-3",
            prompt=full_prompt,
            size="1024x1024",
            response_format="url"
        )
        image_url = response['data'][0]['url']
        print(f"✅ 產生圖片成功：{image_url}")
        
        # 儲存圖片 URL
        if user_id not in story_image_urls:
            story_image_urls[user_id] = {}
        story_image_urls[user_id][prompt] = image_url  # 儲存每個用戶的圖片 URL 和 prompt
        
        # 下載並儲存圖片到本地
        try:
            # 顯示當前工作目錄
            current_dir = os.getcwd()
            print(f"📁 當前工作目錄：{current_dir}")
            
            # 建立 images 資料夾（如果不存在）
            images_dir = os.path.join(current_dir, 'images')
            print(f"📁 嘗試建立資料夾：{images_dir}")
            
            if not os.path.exists(images_dir):
                os.makedirs(images_dir)
                print(f"✅ 成功建立 images 資料夾")
            else:
                print(f"ℹ️ images 資料夾已存在")
            
            # 產生唯一的檔案名稱
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = os.path.join(images_dir, f"{prompt[:30]}_{timestamp}.png")
            print(f"📄 準備儲存檔案：{filename}")
            
            # 下載並儲存圖片
            print("⬇️ 開始下載圖片...")
            img_data = requests.get(image_url).content
            print("✅ 圖片下載完成")
            
            print("💾 開始儲存檔案...")
            with open(filename, "wb") as f:
                f.write(img_data)
            print(f"✅ 圖片已儲存到本地：{filename}")
            
            # 確認檔案是否存在
            if os.path.exists(filename):
                print(f"✅ 確認檔案已建立：{filename}")
            else:
                print(f"❌ 檔案未成功建立：{filename}")
            
        except Exception as e:
            print(f"❌ 儲存本地圖片失敗：{e}")
            traceback.print_exc()
        
        return image_url
    except Exception as e:
        print("❌ 產生圖片失敗：", e)
        traceback.print_exc()
        return None

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
