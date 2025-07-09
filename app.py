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
import random

sys.stdout.reconfigure(encoding='utf-8')
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
story_paragraphs = {}
illustration_mode = {}
practice_mode = {}

@app.route("/")
def index():
    return "LINE GPT Webhook is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)

        events = json.loads(body).get("events", [])
        for event in events:
            if event.get("type") == "message":
                user_id = event["source"]["userId"]
                if user_id not in user_sessions:
                    reset_story_memory(user_id)
                    print(f"👋 使用者 {user_id} 第一次互動，自動進入練習模式")

    except InvalidSignatureError:
        abort(400)
    return "OK"

def reset_story_memory(user_id):
    user_sessions[user_id] = {"messages": []}
    user_message_counts[user_id] = 0
    story_summaries[user_id] = ""
    story_titles[user_id] = ""
    story_image_prompts[user_id] = ""
    story_image_urls[user_id] = {}
    story_current_paragraph[user_id] = 0
    story_paragraphs[user_id] = []
    illustration_mode[user_id] = False
    practice_mode[user_id] = True
    print(f"✅ 使用者 {user_id} 的故事記憶已重置並啟用練習模式")

def generate_story_summary(messages):
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

def extract_story_paragraphs(summary):
    paragraphs = [p.strip() for p in summary.split('\n') if p.strip()]
    clean_paragraphs = [re.sub(r'^\d+\.\s*', '', p) for p in paragraphs]
    return clean_paragraphs[:5]

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    reply_token = event.reply_token
    print(f"📩 收到使用者 {user_id} 的訊息：{user_text}")
    print(f"🔍 目前 practice_mode: {practice_mode.get(user_id)}, illustration_mode: {illustration_mode.get(user_id)}")

    try:
        # --- 偵測「一起來講故事吧」指令，切換到正式創作階段 ---
        if "一起來講故事吧" in user_text:
            reset_story_memory(user_id)
            practice_mode[user_id] = False
            illustration_mode[user_id] = False
            story_current_paragraph[user_id] = 0
            story_paragraphs[user_id] = []
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="太好了，我們開始講故事囉！請告訴我第一段故事內容，每次只說一段，等你說下一段時再繼續。")
            )
            return

        # 練習模式：用戶要求畫圖，直接進入插圖流程
        if practice_mode.get(user_id, False):
            if re.search(r'第[一二三四五12345]段', user_text) or re.search(r"(?:請畫|幫我畫|生成.*圖片|畫.*圖|我想要一張.*圖)", user_text):
                practice_mode[user_id] = False
                illustration_mode[user_id] = True
                story_current_paragraph[user_id] = 0
                # 直接執行插圖流程
                # 直接跳到插圖流程，不回覆多餘提示
            else:
                assistant_reply = get_openai_response(user_id, user_text)
                line_bot_api.reply_message(reply_token, TextSendMessage(text=assistant_reply))
                save_to_firebase(user_id, "user", user_text)
                save_to_firebase(user_id, "assistant", assistant_reply)
                return

        # 若故事段落已經集滿五段，自動進入插圖模式，推送第一段內容
        if not illustration_mode.get(user_id, False) and len(story_paragraphs.get(user_id, [])) == 5:
            illustration_mode[user_id] = True
            story_current_paragraph[user_id] = 0
            first_paragraph = story_paragraphs[user_id][0]
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(
                    text=f"故事完成囉！我們可以開始生成插圖了。\n\n第一段故事是：\n{first_paragraph}\n\n你可以描述這張圖上有什麼元素，或直接說「幫我畫」我也會自動生成！"
                )
            )
            return

        # 正式故事創作階段：逐段收集
        if not illustration_mode.get(user_id, False):
            current_paragraph = story_current_paragraph.get(user_id, 0)
            if current_paragraph >= 5:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="五段故事已經完成囉！如果想畫插圖，可以說「幫我畫第X段」或「請畫…」"))
                return

            # 檢查是否是要求畫圖指令
            match = re.search(r"(?:請畫|幫我畫|生成.*圖片|畫.*圖|我想要一張.*圖)(.*)", user_text)
            if match:
                illustration_mode[user_id] = True
                handle_message(event)
                return

            # 收集故事段落
            if user_id not in story_paragraphs:
                story_paragraphs[user_id] = []
            if len(story_paragraphs[user_id]) < current_paragraph + 1:
                story_paragraphs[user_id].append(user_text)
            else:
                story_paragraphs[user_id][current_paragraph] = user_text

            save_to_firebase(user_id, "user", user_text)

            if len(story_paragraphs[user_id]) == 5:
                # 自動總結
                story_summaries[user_id] = "\n".join(story_paragraphs[user_id])
                illustration_mode[user_id] = True
                story_current_paragraph[user_id] = 0
                first_paragraph = story_paragraphs[user_id][0]
                line_bot_api.reply_message(
                    reply_token,
                    TextSendMessage(
                        text=f"故事完成囉！我們可以開始生成插圖了。\n\n第一段故事是：\n{first_paragraph}\n\n你可以描述這張圖上有什麼元素，或直接說「幫我畫」我也會自動生成！"
                    )
                )
                return
            else:
                current_paragraph = story_current_paragraph[user_id]
                if current_paragraph < 4:
                    prompt = [
                        "謝謝你分享這一段故事！",
                        f"請繼續說第{current_paragraph+2}段故事內容，等你說下一段時再繼續喔。"
                    ]
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="\n".join(prompt)))
                    story_current_paragraph[user_id] = current_paragraph + 1
                else:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="五段故事已完成！如果想畫插圖，可以說「幫我畫第X段」或「請畫…」"))
                    story_current_paragraph[user_id] = 5
                return

        # 插圖生成階段
        if illustration_mode.get(user_id, False):
            match = re.search(r"(?:請畫|幫我畫|生成.*圖片|畫.*圖|我想要一張.*圖)?(?:第([一二三四五12345])段)?(.*)", user_text)
            current_paragraph = story_current_paragraph.get(user_id, 0)
            manual_select = False
            extra_desc = ""
            if match:
                # 解析段落
                paragraph_group = match.group(1)
                extra_desc = match.group(2).strip()
                if paragraph_group:
                    chinese_to_number = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5}
                    num_char = paragraph_group
                    if num_char in chinese_to_number:
                        current_paragraph = chinese_to_number[num_char] - 1
                    else:
                        current_paragraph = int(num_char) - 1
                    manual_select = True

            # 取出該段故事內容
            if user_id not in story_paragraphs or len(story_paragraphs[user_id]) <= current_paragraph:
                line_bot_api.reply_message(reply_token, TextSendMessage(text=f"你還沒說過第{current_paragraph+1}段故事內容，請先補上再畫圖喔！"))
                return
            story_content = story_paragraphs[user_id][current_paragraph]
            # 組裝 prompt
            if extra_desc:
                final_prompt = f"{story_content}。{extra_desc}"
            else:
                final_prompt = story_content

            image_url = generate_dalle_image(final_prompt, user_id)
            print(f"產生圖片 URL: {image_url}")

            if image_url:
                reply_messages = [
                    TextSendMessage(text=f"這是第 {current_paragraph + 1} 段故事的插圖："),
                    ImageSendMessage(original_content_url=image_url, preview_image_url=image_url),
                    TextSendMessage(text="你覺得這張插圖怎麼樣？需要調整嗎？")
                ]
                # 自動推下一段
                next_paragraph = current_paragraph + 1
                if next_paragraph < 5 and user_id in story_paragraphs and len(story_paragraphs[user_id]) >= 5:
                    next_story_content = story_paragraphs[user_id][next_paragraph]
                    next_prompt = (
                        f"接下來是第{next_paragraph+1}段故事：\n{next_story_content}\n\n"
                        "請描述你想要看到哪些畫面元素，或直接說「幫我畫」我也會自動生成！"
                    )
                    reply_messages.append(TextSendMessage(text=next_prompt))
                    story_current_paragraph[user_id] = next_paragraph
                elif next_paragraph >= 5:
                    reply_messages.append(TextSendMessage(text="太好了！所有段落的插圖都完成了！"))
                    illustration_mode[user_id] = False

                line_bot_api.reply_message(reply_token, reply_messages)
                save_to_firebase(user_id, "user", user_text)
                for msg in reply_messages:
                    if isinstance(msg, TextSendMessage):
                        save_to_firebase(user_id, "assistant", msg.text)
                    elif isinstance(msg, ImageSendMessage):
                        save_to_firebase(user_id, "assistant", f"[圖片] {msg.original_content_url}")
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪畫不出這張圖，試試其他描述看看 🖍️"))
            return

        # 其他狀況：一般對話
        assistant_reply = get_openai_response(user_id, user_text)
        if not assistant_reply:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪暫時卡住了，請稍後再試 🌧️"))
            return

        line_bot_api.reply_message(reply_token, TextSendMessage(text=assistant_reply))
        save_to_firebase(user_id, "user", user_text)
        save_to_firebase(user_id, "assistant", assistant_reply)

    except Exception as e:
        print("❌ 發生錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪出了一點小狀況，請稍後再試 🙇"))

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
你是「小繪」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助一位 50 歲以上的長輩創作 5 段故事繪本。
請用簡潔、好讀的語氣回應，每則訊息盡量不超過 35 字並適當分段。

第一階段：故事創作引導，請以「如果我有一個超能力」為主題，引導使用者想像一位主角、他擁有什麼超能力、他在哪裡、遇到什麼事件、解決了什麼問題等，故事共五段。每次請你只回應一段故事，不要一次補完全部段落，等使用者回覆後再繼續進行下一段。請等待使用者輸入下一段內容。

第二階段：插圖引導，幫助使用者描述畫面，生成的插圖上不要有故事的文字，並在完成後詢問是否需調整。

請自稱「小繪」，以朋友般的語氣陪伴使用者完成創作。
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

    low_engagement_inputs = ["不知道", "沒靈感", "嗯", "算了", "不想說", "先跳過", "跳過這題"]
    if any(phrase in user_message.strip().lower() for phrase in low_engagement_inputs):
        assistant_reply = random.choice([
            "沒關係，我們可以慢慢想 👣",
            "如果不想說，我們可以跳過喔 🙂",
            "不用急～你已經很棒了 💪"
        ])
        user_sessions[user_id]["messages"].append({"role": "user", "content": user_message})
        user_sessions[user_id]["messages"].append({"role": "assistant", "content": assistant_reply})
        return assistant_reply

    user_sessions[user_id]["messages"].append({"role": "user", "content": user_message})
    user_message_counts[user_id] += 1

    if user_message_counts[user_id] % 6 == 0:
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

    current_paragraph = story_current_paragraph.get(user_id, 0)
    prompt_with_summary += f"\n\n現在是第{current_paragraph+1}段，請一次只寫一段故事，不要一次補完全部段落，等使用者輸入下一段內容再繼續。"

    encouragement_suffix = random.choice([
        "你剛剛的描述真的很棒喔 🌟",
        "我喜歡你用的那個比喻 👏",
        "慢慢來，小繪在這裡陪你 😊"
    ])

    recent_history = user_sessions[user_id]["messages"][-70:]
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

        if "故事名稱" not in assistant_reply and "總結" not in assistant_reply:
            assistant_reply += f"\n\n{encouragement_suffix}"

        user_sessions[user_id]["messages"].append({"role": "assistant", "content": assistant_reply})

        if user_message_counts[user_id] == 30:
            summary = extract_summary_from_reply(assistant_reply)
            title = extract_title_from_reply(assistant_reply)
            story_summaries[user_id] = summary
            story_titles[user_id] = title
            story_image_prompts[user_id] = f"故事名稱：{title}，主題是：{summary}"

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
        print(f"🖍️ 產生圖片中：{prompt}")
        enhanced_prompt = f"""
{prompt}
No text, no words, no letters, no captions, no numbers, no Chinese or English characters, no signage, no handwriting, no subtitles, no labels, no written language, no symbols, no logos, no watermark, only illustration.
請不要在圖片中加入任何文字、標題、數字、標誌、字幕、說明、書名、描述、手寫字、符號或水印，只要純粹的插畫畫面。
""".strip()
        response = openai.Image.create(
            model="dall-e-3",
            prompt=enhanced_prompt,
            size="1024x1024",
            response_format="url"
        )
        image_url = response['data'][0]['url']
        print(f"✅ 產生圖片成功：{image_url}")

        if user_id not in story_image_urls:
            story_image_urls[user_id] = {}
        if prompt not in story_image_urls[user_id]:
            story_image_urls[user_id][prompt] = []
        story_image_urls[user_id][prompt].append(image_url)
        
        try:
            print("⬇️ 開始下載圖片...")
            img_data = requests.get(image_url).content
            print("✅ 圖片下載完成")
            print("💾 開始上傳到 Imgur...")
            img_base64 = base64.b64encode(img_data).decode('utf-8')
            url = "https://api.imgur.com/3/image"
            headers = {
                "Authorization": f"Client-ID {IMGUR_CLIENT_ID}"
            }
            data = {
                "image": img_base64,
                "type": "base64",
                "privacy": "hidden"
            }
            response = requests.post(url, headers=headers, data=data)
            response_data = response.json()
            if response.status_code == 200 and response_data['success']:
                imgur_url = response_data['data']['link']
                deletehash = response_data['data']['deletehash']
                print(f"✅ 圖片已上傳到 Imgur：{imgur_url}")
                user_doc_ref = db.collection("users").document(user_id)
                user_doc_ref.collection("images").add({
                    "url": imgur_url,
                    "deletehash": deletehash,
                    "prompt": prompt,
                    "timestamp": firestore.SERVER_TIMESTAMP
                })
                print("✅ 圖片資訊已儲存到 Firestore")
                return imgur_url
            else:
                print(f"❌ Imgur API 回應錯誤：{response_data}")
                return image_url
        except Exception as e:
            print(f"❌ 上傳圖片到 Imgur 失敗：{e}")
            traceback.print_exc()
            return image_url
    except Exception as e:
        print("❌ 產生圖片失敗：", e)
        traceback.print_exc()
        return None

@app.route("/story/<user_id>")
def view_story(user_id):
    try:
        user_doc_ref = db.collection("users").document(user_id)
        images = user_doc_ref.collection("images").order_by("timestamp").get()
        chat = user_doc_ref.collection("chat").order_by("timestamp").get()
        story_data = {
            "title": story_titles.get(user_id, "我們的故事"),
            "summary": story_summaries.get(user_id, ""),
            "images": [],
            "content": []
        }
        for img in images:
            story_data["images"].append({
                "url": img.get("url"),
                "prompt": img.get("prompt")
            })
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
        user_doc_ref = db.collection("users").document(user_id)
        images = user_doc_ref.collection("images").order_by("timestamp").get()
        chat = user_doc_ref.collection("chat").order_by("timestamp").get()
        story_data = {
            "title": story_titles.get(user_id, "我們的故事"),
            "summary": story_summaries.get(user_id, ""),
            "images": [],
            "content": []
        }
        for img in images:
            story_data["images"].append({
                "url": img.get("url"),
                "prompt": img.get("prompt")
            })
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
