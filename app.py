from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import openai
import os
import firebase_admin
from firebase_admin import credentials, firestore

# 初始化 Flask app
app = Flask(__name__)

# 讀取環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_TOKEN = os.environ.get("FIREBASE_TOKEN")

# 初始化 LINE Bot 與 OpenAI
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

# 初始化 Firebase
cred = credentials.Certificate(FIREBASE_TOKEN)
firebase_admin.initialize_app(cred)
db = firestore.client()

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

# 接收訊息事件
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()

    # Firebase 限制：只記錄前8位使用者
    user_ref = db.collection("users")
    user_ids = [doc.id for doc in user_ref.stream()]
    if user_id not in user_ids and len(user_ids) >= 8:
        reply_text = "很抱歉，目前名額已滿，小頁無法記錄更多使用者的故事 😢"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    if user_id not in user_ids:
        user_ref.document(user_id).set({"created": firestore.SERVER_TIMESTAMP})

    # 檢查是否為繪圖請求
    if user_text.startswith("請畫"):
        prompt = user_text.replace("請畫", "").strip()

        try:
            image_response = openai.Image.create(
                prompt=prompt,
                n=1,
                size="512x512"
            )
            image_url = image_response['data'][0]['url']

            # 儲存訊息到 Firebase
            db.collection("users").document(user_id).collection("messages").add({
                "from": "user",
                "type": "text",
                "content": user_text
            })
            db.collection("users").document(user_id).collection("messages").add({
                "from": "bot",
                "type": "image",
                "content": image_url
            })

            line_bot_api.reply_message(
                event.reply_token,
                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
            )
        except Exception as e:
            error_msg = f"發生錯誤：{str(e)}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=error_msg))
        return

    # 否則為文字對話，呼叫 GPT
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "你是一位親切、有耐心且擅長說故事的 AI 夥伴，名字叫小頁。...（此處省略系統 prompt，可接續你現有的）"},
                {"role": "user", "content": user_text}
            ],
            max_tokens=500
        )
        reply_text = response['choices'][0]['message']['content'].strip()

        # 儲存訊息到 Firebase
        db.collection("users").document(user_id).collection("messages").add({
            "from": "user",
            "type": "text",
            "content": user_text
        })
        db.collection("users").document(user_id).collection("messages").add({
            "from": "bot",
            "type": "text",
            "content": reply_text
        })

    except Exception as e:
        reply_text = f"發生錯誤：{str(e)}"

    # 回傳訊息給 LINE 使用者
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
