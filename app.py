import sys
import os
import traceback
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import openai

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore

sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__)

# 環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_TOKEN_PATH = os.environ.get("FIREBASE_TOKEN_PATH")

# 初始化 LINE / OpenAI
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

# 初始化 Firebase
if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_TOKEN_PATH)
    firebase_admin.initialize_app(cred)
db = firestore.client()

@app.route("/")
def index():
    return "LINE GPT Webhook is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    user_id = event.source.user_id

    # 儲存 User ID
    try:
        db.collection("users").document(user_id).set({"USERID": user_id})
    except Exception as e:
        print(f"⚠️ Firebase 寫入錯誤：{e}")

    try:
        if user_text.startswith("請畫"):
            prompt = user_text.replace("請畫", "").strip()

            dalle_response = openai.Image.create(
                prompt=prompt,
                n=1,
                size="1024x1024"
            )
            image_url = dalle_response["data"][0]["url"]

            # 回覆圖片訊息
            line_bot_api.reply_message(
                event.reply_token,
                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
            )

            # 儲存圖片訊息到 Firestore
            db.collection("users").document(user_id).collection("messages").add({
                "timestamp": datetime.utcnow(),
                "type": "image",
                "content": image_url
            })
            return

        # 非繪圖請求：使用 ChatGPT
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {
                    "role": "system",
                    "content": "（此處保留你原本的 prompt，為了簡潔未重複貼上）"
                },
                {"role": "user", "content": user_text}
            ],
            max_tokens=500
        )

        reply_text = response['choices'][0]['message']['content'].strip()

        # 回覆文字
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )

        # 儲存文字訊息到 Firestore（包含 user 的訊息與機器人回覆）
        db.collection("users").document(user_id).collection("messages").add({
            "timestamp": datetime.utcnow(),
            "type": "text",
            "content": f"使用者說：{user_text}"
        })
        db.collection("users").document(user_id).collection("messages").add({
            "timestamp": datetime.utcnow(),
            "type": "text",
            "content": f"小頁回覆：{reply_text}"
        })

    except Exception as e:
        error_details = traceback.format_exc()
        print("⚠️ OpenAI API 發生錯誤：\n", error_details)
        fallback = "小頁剛才有點迷路了，能再說一次看看嗎？😊"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=fallback)
        )

        db.collection("users").document(user_id).collection("messages").add({
            "timestamp": datetime.utcnow(),
            "type": "text",
            "content": "⚠️ 系統錯誤，已回應 fallback"
        })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
