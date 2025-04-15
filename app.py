import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

# Firebase 初始化
cred = credentials.Certificate("serviceAccountKey.json")  # 放你的路徑
firebase_admin.initialize_app(cred)
db = firestore.client()

# Flask & LINE 初始化
app = Flask(__name__)
line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
openai.api_key = os.environ.get("OPENAI_API_KEY")

@app.route("/")
def index():
    return "LINE GPT Firebase Bot is running!"

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
    timestamp = datetime.utcnow()

    # Firebase 使用者限制：只記錄前 8 位使用者
    user_ref = db.collection("users").document(user_id)
    all_users = db.collection("users").get()
    if not user_ref.get().exists and len(all_users) >= 8:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="目前僅開放前8位使用者參與，請稍後再試 🙏")
        )
        return

    response_text = ""
    image_url = None

    # 若以「請畫」開頭 → DALL·E 畫圖
    if user_text.startswith("請畫"):
        prompt = user_text.replace("請畫", "").strip()
        try:
            img_response = openai.Image.create(prompt=prompt, n=1, size="512x512")
            image_url = img_response['data'][0]['url']
            response_text = "[圖片已生成]"
            line_bot_api.reply_message(
                event.reply_token,
                ImageSendMessage(
                    original_content_url=image_url,
                    preview_image_url=image_url
                )
            )
        except Exception as e:
            response_text = f"圖片生成錯誤：{str(e)}"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=response_text)
            )
    else:
        # GPT 回應文字
        try:
            chat_response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "你是一位友善的助理。"},
                    {"role": "user", "content": user_text}
                ]
            )
            response_text = chat_response['choices'][0]['message']['content'].strip()
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=response_text)
            )
        except Exception as e:
            response_text = f"文字回應錯誤：{str(e)}"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=response_text)
            )

    # 將對話記錄存入 Firebase
    user_ref.set({"active": True}, merge=True)
    db.collection("users").document(user_id).collection("messages").add({
        "timestamp": timestamp,
        "from_user": user_text,
        "from_bot": response_text,
        "image_url": image_url
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
