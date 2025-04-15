# 🔁 修改版本：app.py
import sys
import os
import json
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

# 解決中文錯誤訊息編碼問題
sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__)

# 環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS")

# 初始化 LINE / OpenAI
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

# ====== 儲存訊息到 Firestore 中每位用戶單獨的 document 中 ======
def log_message_to_firestore(user_id, user_msg, bot_reply):
    try:
        doc_ref = db.collection("users").document(user_id)
        doc = doc_ref.get()
        if not doc.exists:
            doc_ref.set({
                "USERID": user_id,
                "history": [
                    {"role": "system", "content": SYSTEM_PROMPT}
                ]
            })

        doc_ref.update({
            "history": firestore.ArrayUnion([
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": bot_reply}
            ])
        })
        print("✅ 對話記錄已寫入 Firebase")
    except Exception as e:
        print(f"⚠️ Firebase 寫入錯誤：{e}")

def get_full_history(user_id):
    try:
        doc = db.collection("users").document(user_id).get()
        if doc.exists:
            return doc.to_dict().get("history", [])
        else:
            return [{"role": "system", "content": SYSTEM_PROMPT}]
    except Exception as e:
        print(f"⚠️ 無法讀取歷史：{e}")
        return [{"role": "system", "content": SYSTEM_PROMPT}]

# ====== 系統提示語 ======
SYSTEM_PROMPT = """你是一位親切、有耐心且擅長說故事的 AI 夥伴，名字叫 小頁。
（中略，請保留完整內容）
"""

# 處理訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text
    user_id = event.source.user_id
    reply_token = event.reply_token

    timestamp = datetime.utcnow().isoformat()

    # 圖片請求
    if user_text.startswith("請畫"):
        prompt = user_text.replace("請畫", "").strip()
        try:
            dalle_response = openai.Image.create(
                prompt=prompt,
                n=1,
                size="1024x1024"
            )
            image_url = dalle_response['data'][0]['url']

            # 傳送圖片
            line_bot_api.reply_message(
                reply_token,
                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
            )

            # Firebase 儲存圖片訊息
            db.collection("users").document(user_id).update({
                "history": firestore.ArrayUnion([
                    {"role": "user", "content": f"請畫 {prompt}"},
                    {"role": "assistant", "content": f"[圖片生成連結]({image_url})"}
                ])
            })

            return

        except Exception:
            print("⚠️ DALL·E 發生錯誤：", traceback.format_exc())
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="圖片生成時出了一點問題，請再試一次 🥲")
            )
            return

    # 對話模式
    try:
        history = get_full_history(user_id)
        history.append({"role": "user", "content": user_text})

        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=history,
            max_tokens=500
        )

        reply_text = response['choices'][0]['message']['content'].strip()

        # 回覆使用者
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=reply_text)
        )

        # 寫入 Firebase
        log_message_to_firestore(user_id, user_text, reply_text)

    except Exception as e:
        error_details = traceback.format_exc()
        print("⚠️ OpenAI API 發生錯誤：\n", error_details)
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="小頁剛才有點迷路了，能再說一次看看嗎？😊")
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
