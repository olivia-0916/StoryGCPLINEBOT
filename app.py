import sys
import os
import json
import traceback
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import openai

# Firebase（註解中）
# import firebase_admin
# from firebase_admin import credentials, firestore

sys.stdout.reconfigure(encoding='utf-8')
app = Flask(__name__)

# ====== 環境變數讀取 ======
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")

# ====== 初始化 LINE / OpenAI ======
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

# ====== Firebase 初始化（註解中） ======
# def get_firebase_credentials_from_env():
#     return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))

# firebase_admin.initialize_app(get_firebase_credentials_from_env())
# db = firestore.client()

# ====== 儲存會話狀態 ======
user_sessions = {}

# ====== 首頁測試路由 ======
@app.route("/")
def index():
    return "LINE GPT Webhook is running!"

# ====== LINE Webhook 路由 ======
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ====== 處理訊息事件 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text
    user_id = event.source.user_id
    reply_token = event.reply_token
    print(f"📥 收到訊息：{user_text}")

    try:
        # === Firebase token 判斷與儲存（註解中） ===
        # token_ref = db.collection("processed_tokens").document(reply_token)
        # if token_ref.get().exists:
        #     return
        # else:
        #     token_ref.set({"handled": True})

        # user_doc_ref = db.collection("users").document(user_id)
        # user_doc_ref.set({"updated_at": firestore.SERVER_TIMESTAMP}, merge=True)

        # === 取得 GPT 回應 ===
        assistant_reply = get_openai_response(user_id, user_text)
        if not assistant_reply:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="我遇到一點問題，請稍後再試～"))
            return

        # === 回覆使用者 ===
        line_bot_api.reply_message(reply_token, TextSendMessage(text=assistant_reply))

        # === 儲存對話到 Firebase（註解中） ===
        # convo_ref = user_doc_ref.collection("messages").document("conversation")
        # convo_ref.set({
        #     "history": firestore.ArrayUnion([{
        #         "user": user_text,
        #         "assistant": assistant_reply,
        #         "timestamp": firestore.SERVER_TIMESTAMP
        #     }])
        # }, merge=True)

        return  # 確保結束處理

    except Exception as e:
        print("❌ 錯誤處理訊息：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，我出了點問題 🙇"))
        return

# ====== GPT 回應邏輯 ======
def get_openai_response(user_id, user_message):
    # 初始化使用者狀態
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "system_prompt": """你是「小頁」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助一位 50 歲以上的長輩創作 5 段故事繪本。
請用簡潔、好讀的語氣回應，每則訊息盡量不超過 35 字並適當分段。
🌱 第一階段：故事創作引導
引導使用者想像角色、場景與情節，發展成五段故事。每次回覆後，請簡要整理目前的段落並提醒進度。
不要主導故事，保持引導與陪伴。
🎨 第二階段：插圖引導
插圖風格溫馨童趣、色彩柔和、畫面簡單。
幫助使用者描述畫面，並在完成後詢問是否需調整。
請自稱「小頁」，以朋友般的語氣陪伴使用者完成創作。""",
            "first_interaction": True
        }

    session = user_sessions[user_id]

    if session["first_interaction"]:
        messages = [
            {"role": "system", "content": session["system_prompt"]},
            {"role": "user", "content": user_message}
        ]
        session["first_interaction"] = False
    else:
        messages = [
            {"role": "user", "content": user_message}
        ]

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=60,  # 大約 35 中文字（1 字 ≈ 2 tokens）
            temperature=0.7
        )
        return response.choices[0].message["content"]
    except Exception as e:
        print("❌ OpenAI 錯誤：", e)
        traceback.print_exc()
        return None

# ====== 運行應用程式 ======
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
