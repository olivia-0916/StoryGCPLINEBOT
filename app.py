import sys
import os
import json
import traceback
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
app = Flask(__name__)

# ====== 環境變數 ======
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

# ====== Firebase 初始化 ======
def get_firebase_credentials_from_env():
    service_account_info = json.loads(FIREBASE_CREDENTIALS_JSON)
    print("✅ 成功從環境變數讀取 Firebase 金鑰")
    return credentials.Certificate(service_account_info)

firebase_cred = get_firebase_credentials_from_env()
firebase_admin.initialize_app(firebase_cred)
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

# ====== 處理訊息事件 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    user_id = event.source.user_id
    timestamp = datetime.utcnow()

    # 儲存使用者 ID
    try:
        db.collection("users").document(user_id).set({"USERID": user_id}, merge=True)
    except Exception as e:
        print(f"⚠️ Firebase 寫入使用者失敗：{e}")

    # 🔥 DALL·E 畫圖功能：「請畫」開頭時觸發
    if user_text.startswith("請畫"):
        prompt = user_text[2:].strip()
        try:
            dalle_response = openai.Image.create(
                prompt=prompt,
                n=1,
                size="512x512"
            )
            image_url = dalle_response['data'][0]['url']
            # 回傳圖片訊息
            line_bot_api.reply_message(
                event.reply_token,
                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
            )
            # 🔥 將圖片 URL 寫入 Firebase
            db.collection("users").document(user_id).collection("messages").add({
                "timestamp": timestamp,
                "type": "image",
                "prompt": prompt,
                "image_url": image_url
            })
            return
        except Exception as e:
            print("⚠️ DALL·E 錯誤：", traceback.format_exc())
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="小頁畫圖時遇到一點問題，待會再試試看好嗎？🎨")
            )
            return

    # 🔥 GPT-4 回覆對話邏輯
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {
                    "role": "system",
                    "content": "（省略系統訊息，你可以貼回原本的 story prompt）"
                },
                {"role": "user", "content": user_text}
            ],
            max_tokens=500
        )
        reply_text = response['choices'][0]['message']['content'].strip()
    except Exception as e:
        print("⚠️ OpenAI 錯誤：", traceback.format_exc())
        reply_text = "小頁剛才有點迷路了，能再說一次看看嗎？😊"

    # 回傳文字訊息
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

    # 🔥 儲存文字訊息與回覆到 Firebase
    try:
        db.collection("users").document(user_id).collection("messages").add({
            "timestamp": timestamp,
            "type": "text",
            "user_input": user_text,
            "bot_reply": reply_text
        })
    except Exception as e:
        print(f"⚠️ Firebase 儲存訊息失敗：{e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
