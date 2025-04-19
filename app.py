import sys
import os
import json
import traceback
import re
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import openai

# === Firebase ===
import firebase_admin
from firebase_admin import credentials, firestore

# === Python 編碼設定（for Windows）===
sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__)
print("✅ Flask App initialized")


# === 環境變數設定 ===
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")

# === 初始化 LINE / OpenAI ===
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

# === 初始化 Firebase ===
def get_firebase_credentials_from_env():
    return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))

firebase_admin.initialize_app(get_firebase_credentials_from_env())
db = firestore.client()

# === 儲存每位使用者的 session ===
user_sessions = {}
user_message_counts = {}
story_summaries = {}

# === 首頁測試用 ===
@app.route("/")
def index():
    return "LINE GPT Webhook is running!"

# === LINE Webhook ===
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# === 處理 LINE 訊息 ===
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    reply_token = event.reply_token

    print(f"📩 收到使用者 {user_id} 的訊息：{user_text}")

    try:
        # ✅ 判斷是否包含請求畫圖的語句
        if re.search(r"(請畫|幫我畫|生成.*圖片|幫我生成.*圖片|畫.*圖|我想要一張.*圖)", user_text):
            prompt = re.sub(r"(請畫|幫我畫|請幫我畫|幫我生成|請幫我生成|我想要一張)", "", user_text)
            prompt = re.sub(r"(的圖片|圖片|的圖|圖)", "", prompt).strip()

            image_url = generate_dalle_image(prompt)

            if image_url:
                line_bot_api.reply_message(
                    reply_token,
                    ImageSendMessage(
                        original_content_url=image_url,
                        preview_image_url=image_url
                    )
                )
                print("✅ 已傳送圖片")
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="小頁畫不出這張圖，試試其他描述看看 🖍️"))
            return

        # 否則照一般流程處理訊息
        assistant_reply = get_openai_response(user_id, user_text)
        if not assistant_reply:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="小頁暫時卡住了，請稍後再試 🌧️"))
            return

        line_bot_api.reply_message(reply_token, TextSendMessage(text=assistant_reply))
        print("✅ 已回覆 LINE 使用者")

        # 儲存到 Firebase
        save_to_firebase(user_id, "user", user_text)
        save_to_firebase(user_id, "assistant", assistant_reply)

    except Exception as e:
        print("❌ 發生錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="小頁出了一點小狀況，請稍後再試 🙇"))


# === 儲存訊息到 Firebase ===
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


# 全域變數：記錄使用者的 user message 次數
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
    # 將中文句號、問號、驚嘆號後面加換行
    return re.sub(r'([。！？])\s*', r'\1\n', text)
    
# 儲存使用者的歷史訊息
def get_openai_response(user_id, user_message):
    if user_id not in user_sessions:
        user_sessions[user_id] = {"messages": []}
    if user_id not in user_message_counts:
        user_message_counts[user_id] = 0
    if user_id not in story_summaries:
        story_summaries[user_id] = ""

    user_sessions[user_id]["messages"].append({
        "role": "user",
        "content": user_message
    })
    user_message_counts[user_id] += 1

    # 每 5 次發言後總結一次故事
    if user_message_counts[user_id] % 5 == 0:
        global base_system_prompt
        base_system_prompt += "\n請在這次回覆後，用 150 字內簡要總結目前的故事內容（不用重複細節），之後我會將這個摘要提供給你作為背景，請延續故事創作。"

    summary_context = story_summaries[user_id]
    if summary_context:
        base_system_prompt += f"\n\n【故事摘要】\n{summary_context}\n請根據以上摘要，延續創作對話內容。"

    recent_history = user_sessions[user_id]["messages"][-5:]
    messages = [{"role": "system", "content": base_system_prompt}] + recent_history

    try:
        print(f"📦 傳給 OpenAI 的訊息：{json.dumps(messages, ensure_ascii=False)}")
        print(f"🧪 使用的 OpenAI Key 開頭：{openai.api_key[:10]}")

        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.7,
        )
        assistant_reply = response.choices[0].message["content"]
        assistant_reply = format_reply(assistant_reply)  # 👈 加這一行
        

        user_sessions[user_id]["messages"].append({
            "role": "assistant",
            "content": assistant_reply
        })

        if user_message_counts[user_id] % 5 == 0:
            story_summaries[user_id] = extract_summary_from_reply(assistant_reply)

        return assistant_reply

    except Exception as e:
        print("❌ OpenAI 回應錯誤：", e)
        traceback.print_exc()
        return None


# 提取摘要的函數
def extract_summary_from_reply(reply_text):
    # 用正則表達式或關鍵字提取摘要段落
    parts = reply_text.strip().split("\n")
    for part in reversed(parts):
        if "這段故事" in part or "總結" in part or "目前的故事內容" in part:
            return part.strip()
    return ""
    
# 產生 DALL·E 圖片
def generate_dalle_image(prompt):
    try:
        print(f"🖼️ 產生圖片中：{prompt}")
        response = openai.Image.create(
            prompt=prompt,
            n=1,
            size="512x512"
        )
        image_url = response['data'][0]['url']
        print(f"✅ 產生圖片成功：{image_url}")
        return image_url
    except Exception as e:
        print("❌ 產生圖片失敗：", e)
        traceback.print_exc()
        return None


# === 啟動 Flask 伺服器 ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
