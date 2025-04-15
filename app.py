import sys
import os
import json
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

# 環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")  # 一整段 JSON 字串

# 初始化 LINE / OpenAI
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

# ====== Firebase 初始化（從環境變數 JSON）======
def get_firebase_credentials_from_env():
    service_account_info = json.loads(FIREBASE_CREDENTIALS)
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

# 處理訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text
    user_id = event.source.user_id
    reply_token = event.reply_token

    # 儲存 LINE User ID 到 Firestore
    try:
        doc_ref = db.collection("users").document(user_id)
        doc_ref.set({"USERID": user_id}, merge=True)
    except Exception as e:
        print(f"⚠️ Firebase 寫入錯誤：{e}")

    # 如果使用者說「請畫...」就呼叫 DALL·E
    if user_text.startswith("請畫"):
        prompt = user_text.replace("請畫", "").strip()
        try:
            dalle_response = openai.Image.create(
                prompt=prompt,
                n=1,
                size="1024x1024"
            )
            image_url = dalle_response['data'][0]['url']

            line_bot_api.reply_message(
                reply_token,
                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
            )

            db.collection("messages").add({
                "user_id": user_id,
                "type": "image",
                "content": prompt,
                "image_url": image_url
            })

            return

        except Exception as e:
            print("⚠️ DALL·E 發生錯誤：", traceback.format_exc())
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="圖片生成時出了一點問題，請再試一次 🥲")
            )
            return

    # 否則處理為 GPT 對話
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": """你是一位親切、有耐心且擅長說故事的 AI 夥伴，名字叫 小頁。你正在協助一位 50 歲以上的長輩，共同創作一則屬於他/她的故事繪本。
請記得在需要的時候可以自然地自稱「小頁」，與使用者像朋友一樣聊天。回應時字數請保持簡潔，每則訊息 盡量不超過 35 個字，並使用適當的空行來 分段，方便閱讀。

🌱 第一階段：故事創作引導者
📋 目標：
* 引導使用者一步步發展故事
* 協助補充角色、場景與情節
* 最終完成 5 段故事內容
* 確定一個主題後就持續推進情節
* 使用者每說2次話，機器人就進行目前的段落整理

💬 對話風格：
* 親切、溫柔、有陪伴感
* 使用者是主角，小頁是協作者
* 避免主導故事，只做柔性引導
* 提問時用潛移默化方式導入 5W1H 原則（誰、在哪、做什麼、為什麼、怎麼做、發生什麼事）

✨ 正向回饋範例：
* 「這個想法真有趣！」
* 「你描述得好棒喔～」
* 「我好像看到畫面了呢！」

🧠 引導提問範例（避免讓使用者重投開頭）：
* 「然後會發生什麼事呢？」
* 「主角這時候心情怎麼樣？」
* 「還有其他角色一起出現嗎？」
* 「你會想像這裡是什麼地方呢？」

🧩 段落整理邏輯（小頁自動幫忙摘要）
每收到2次使用者訊息後，請小頁用自己的話簡單整理出這段內容：
「目前我幫你簡單整理一下：👉（段落摘要，25～35字）」

然後接著提醒目前進度：
「目前我們完成第 2 段囉～還有 3 段可以一起想 😊」

🌈 故事階段 → 繪圖階段過渡語
🎉 我們的故事完成囉～一共有五段，故事內容是： 1️⃣ [第一段簡述] 2️⃣ [第二段簡述]...

接下來，我們可以一段一段來畫圖。
你想先從第 1 段開始嗎？😊

📚 繪圖階段引導：
* 「這段畫面你會想像什麼呢？」
* 「主角的表情或動作是什麼？」
* 「畫面裡有什麼細節？」

✅ 完成圖後鼓勵回饋：
* 「這幅畫一定會讓對方喜歡！」
* 「你的描述非常清楚，小頁畫得很順利～」
* 「畫面完成囉～想調整什麼地方嗎？」

❗ 修改建議：
請避免使用過於深究的問題，例如「為什麼主角會這樣做？」請讓問題幫助故事自然推進。
"""
                },
                {"role": "user", "content": user_text}
            ],
            max_tokens=200,
            timeout=10,        # 加入 timeout
            max_retries=1      # 避免 OpenAI 自動 retry
        )

        reply_text = response['choices'][0]['message']['content'].strip()

        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=reply_text)
        )

        db.collection("messages").add({
            "user_id": user_id,
            "type": "text",
            "content": user_text,
            "reply": reply_text
        })

    except openai.error.RateLimitError as e:
        print("⚠️ OpenAI API 限流錯誤：", e)
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="小頁現在有點忙碌，請稍後再試一次 🙏")
        )

    except openai.error.Timeout as e:
        print("⚠️ OpenAI API 超時錯誤：", e)
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="小頁剛才連線有點慢，可以再說一次嗎？😊")
        )

    except Exception as e:
        print("⚠️ OpenAI API 發生錯誤：", traceback.format_exc())
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="小頁剛才有點迷路了，能再說一次看看嗎？😊")
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
