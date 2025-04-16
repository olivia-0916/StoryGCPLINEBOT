import sys
import os
import json
import traceback
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import openai

# === Firebase ===
import firebase_admin
from firebase_admin import credentials, firestore

# === Python 編碼設定（for Windows）===
sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__)

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
        assistant_reply = get_openai_response(user_id, user_text)
        if not assistant_reply:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="小頁暫時卡住了，請稍後再試 🌧️"))
            return

        # 回覆使用者
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

# === GPT 回應邏輯 ===
def get_openai_response(user_id, user_message):
    system_prompt = """
你是「小頁」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助一位 50 歲以上的長輩創作 5 段故事繪本。
請用簡潔、好讀的語氣回應，每則訊息盡量不超過 35 字並適當分段。
第一階段：故事創作引導，引導使用者想像角色、場景與情節，發展成五段故事。每次回覆後，請簡要整理目前的段落並提醒進度。
不要主導故事，保持引導與陪伴。
第二階段：插圖引導，插圖風格溫馨童趣、色彩柔和、畫面簡單。
幫助使用者描述畫面，並在完成後詢問是否需調整。
請自稱「小頁」，以朋友般的語氣陪伴使用者完成創作。
""".strip()

    # 初始化 session（只執行一次）
    if user_id not in user_sessions:
        user_sessions[user_id] = {"messages": []}

    # 加入使用者訊息
    user_sessions[user_id]["messages"].append({
        "role": "user",
        "content": user_message
    })

    # 擷取最近 20 則非 system 訊息
    history = [
        msg for msg in user_sessions[user_id]["messages"]
        if msg["role"] != "system"
    ][-20:]

    # 建立對話：1 則 system + 最近歷史訊息
    messages = [{"role": "system", "content": system_prompt}] + history

    # 呼叫 OpenAI API
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.7,
        )
        assistant_reply = response.choices[0].message["content"]
        # 加入 AI 回應到 session
        user_sessions[user_id]["messages"].append({
            "role": "assistant",
            "content": assistant_reply
        })
        return assistant_reply

    except Exception as e:
        print("❌ OpenAI 回應錯誤：", e)
        return None

# === 啟動 Flask 伺服器 ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
