import sys
import os
import json
import time
import traceback
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import openai

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore

# 解決中文錯誤訊息編碼問題
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

# ====== Firebase 初始化 ======
def get_firebase_credentials_from_env():
    service_account_info = json.loads(FIREBASE_CREDENTIALS)
    print("✅ 成功從環境變數讀取 Firebase 金鑰")
    return credentials.Certificate(service_account_info)

firebase_cred = get_firebase_credentials_from_env()
firebase_admin.initialize_app(firebase_cred)
db = firestore.client()

# ====== 首頁測試路由 ======
@app.route("/")
def index():
    return "LINE GPT Webhook is running!"

# ====== LINE Webhook 路由 ======
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

# ====== 處理訊息事件 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text
    user_id = event.source.user_id
    reply_token = event.reply_token

    try:
        # === 檢查是否已處理過這個 reply_token ===
        token_ref = db.collection("processed_tokens").document(reply_token)
        if token_ref.get().exists:
            print("⚠️ 已處理過此 reply_token，跳過。")
            return
        else:
            token_ref.set({"handled": True})

        # === 建立或更新使用者 ===
        user_doc = db.collection("users").document(user_id)
        user_doc.set({
            "USERID": user_id,
            "updated_at": firestore.SERVER_TIMESTAMP
        }, merge=True)

        # === 處理圖片訊息 ===
        if user_text.startswith(("請畫", "畫出", "幫我畫")):
            prompt = user_text
            for key in ["請畫", "畫出", "幫我畫"]:
                prompt = prompt.replace(key, "")
            prompt = prompt.strip()

            existing_img = user_doc.collection("messages")\
                .where("type", "==", "image").where("content", "==", prompt).stream()
            if any(existing_img):
                print("⚠️ 重複圖片 prompt，跳過儲存")
                return

            response = openai.Image.create(prompt=prompt, n=1, size="512x512")
            image_url = response["data"][0]["url"]

            line_bot_api.reply_message(
                reply_token,
                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
            )

            user_doc.collection("messages").add({
                "type": "image",
                "content": prompt,
                "image_url": image_url,
                "timestamp": firestore.SERVER_TIMESTAMP
            })
            return

        # === 檢查是否重複文字訊息 ===
        existing_text = user_doc.collection("messages")\
            .where("type", "==", "text").where("content", "==", user_text).stream()
        if any(existing_text):
            print("⚠️ 重複文字訊息，跳過處理")
            return

        # === 呼叫 OpenAI 取得回應 ===
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": user_text}]
        )
        assistant_reply = response["choices"][0]["message"]["content"]

        # === 回傳訊息到 LINE ===
        line_bot_api.reply_message(reply_token, TextSendMessage(text=assistant_reply))

        # === 儲存對話紀錄到 Firebase ===
        user_doc.collection("messages").add({
            "type": "text",
            "content": user_text,
            "reply": assistant_reply,
            "timestamp": firestore.SERVER_TIMESTAMP
        })

    except openai.error.RateLimitError as e:
        print("⚠️ OpenAI API 限流：", e)
        line_bot_api.reply_message(reply_token, TextSendMessage(text="小頁有點忙，稍後再來找我吧～"))

    except openai.error.Timeout as e:
        print("⚠️ OpenAI 超時：", e)
        line_bot_api.reply_message(reply_token, TextSendMessage(text="好像等太久了，能再說一次嗎？"))

    except Exception as e:
        print("⚠️ 發生未知錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="我剛剛迷路了 😢 可以再試一次嗎？"))

# ====== 運行應用程式 ======
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
