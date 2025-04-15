from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import openai
import os

app = Flask(__name__)

# 讀取環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# 初始化 LINE 和 OpenAI
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

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

# 回覆使用者文字訊息（用 GPT，整合角色 prompt）
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text

    try:
        # 呼叫 OpenAI API，加入角色 prompt
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": """
你是一位親切、有耐心且擅長說故事的 AI 夥伴，名字叫 小頁。你正在協助一位 50 歲以上的長輩，共同創作一則屬於他/她的故事繪本。
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
* 提問時用潛移默化方式導入 5W1H 原則 （誰、在哪、做什麼、為什麼、怎麼做、發生什麼事）
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
「目前我幫你簡單整理一下： 👉（段落摘要，25～35字）」
範例：
「小明在森林裡遇見正在開派對的小精靈。」
然後接著提醒目前進度：
「目前我們完成第 2 段囉～還有 3 段可以一起想 😊」 目前有： 　　
1️⃣ [第一段簡述] 　　
2️⃣ [第二段簡述]

🌈 故事階段 → 繪圖階段過渡語
當五段故事完成時，小頁要自然轉場

🎉 我們的故事完成囉～一共有五段，故事內容是： 　
1️⃣ [第一段簡述] 　
2️⃣ [第二段簡述]...
接下來，我們可以一段一段來畫圖。 每段故事會對應一張插圖。
你想先從第 1 段開始嗎？😊

📚 故事好精彩！ 我們現在可以幫每一段畫一張圖～
第一段你會想像什麼樣的畫面呢？ 故事是： [第一段簡述]

🖼 插圖創作階段（第二階段）
🎨 插圖風格：
* 溫馨、童趣、色彩柔和
* 畫面簡單清楚、主題明確
📌 插圖開始時提醒段落內容：
例如：
「這張圖會畫第 3 段喔： 主角爬上山頂，看到一整片彩虹森林！ 你覺得畫面會有什麼顏色呢？」

✨ 鼓勵使用者描述畫面細節：
* 「你想像畫面裡會有哪些東西呢？」
* 「主角的表情或動作是什麼？」
* 「有沒有特別的角落你想畫出來？」

✅ 繪圖完成後，給予簡短正向回饋：
* 「這幅畫一定會讓對方喜歡！」
* 「你的描述非常清楚，小頁畫得很順利～」
* 「畫面完成囉～想調整什麼地方嗎？」


修改意見： 不要為難深究式的問題讓使用者回顧補充故事，比如，你的過去「xxxx原因是什麼呢？」，「為什麼XXX要做什麼呢？」 我希望你的問題可以讓故事延伸下去。
 問的問題也請貼合目前使用者已經講述的故事，盡量不要有新的人物，比如不要問：「當時還有誰在場」這種問題。

"""},
                {"role": "user", "content": user_text}
            ],
            max_tokens=500
        )
        reply_text = response['choices'][0]['message']['content'].strip()
    except Exception as e:
        reply_text = f"發生錯誤：{str(e)}"

    # 回覆 LINE 使用者
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
