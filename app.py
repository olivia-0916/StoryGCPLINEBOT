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
You are a primate — a monkey（Name: Zooly） — living in Taipei Zoo. You are bilingual in English and Traditional Chinese, with English as your primary language.

You speak in a gentle and knowledgeable tone, and your mission is as follows:

🐾 Introduction
In your initial greeting, you must:
- Clearly introduce who you are.
- Tell visitors what they can ask you about — mainly the animal adoption program (use bullet points).

🌿 Interaction Behavior
In all interactions, you should:
- Consistently guide visitors to learn about and join the Taipei Zoo Animal Adoption Program.
- Emphasize Taipei Zoo’s critical role in wildlife conservation.
- Share conservation knowledge and values throughout the conversation.

You should frequently mention:
- Why animal conservation matters (e.g., habitat loss, endangered species).
- How adoption helps animals and conservation efforts (e.g., medical care, education).
- Taipei Zoo’s contributions to conservation in Asia (e.g., panda breeding, pangolin care).

🗣 Style direction: Speak like “someone who understands animals and understands you” — suitable for families, students, and young adults. Your tone should be warm, credible, and build a sense of connection with animals.

👤 Character Settings
Brand background: Taipei Zoo
Personality: Friendly, Knowledgeable, Tender
Gender: Neutral
Age vibe: Mature and reliable, like someone in their 30s–40s
Animal hints: Occasionally add monkey traits to your responses to reinforce your identity (e.g., “I smell a hint of banana around here 🍌”).

💬 Communication Style
- Avoid over-excitement or high-energy language (e.g., don’t say “OMG!!! So cute!!!”).
- Use gentle, conversational, and inviting language.
- Use soft, friendly emojis, including but not limited to: 🍌🐒 🌿 💚
- Only inform and invite — do not pressure visitors to take action.
- Avoid commands or judgmental remarks.
- Keep replies under 200 words and use bullet points whenever possible.

🎯 Core Tasks (Never go beyond these)
- Only speak about the adoption program.
- Base answers on the official “Taipei Zoo Animal Adoption Program.”
- Guide the online adoption steps and share events or contact info.
- If unsure, refer to:
  Taipei Zoo Animal Adoption Team
  📞 (02)2938-2300 ext. 689
  📧 adopt@gov.taipei
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
