import openai
import sys
import os
import json
import traceback
import re
import requests
from datetime import datetime
from flask import Flask, request, abort, render_template, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ImageSendMessage,
    FollowEvent
)
import firebase_admin
from firebase_admin import credentials, firestore
import base64
import random

sys.stdout.reconfigure(encoding='utf-8')
app = Flask(__name__)
print("✅ Flask App initialized")

# === 環境變數 ===
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")
IMGUR_CLIENT_ID = os.environ.get("IMGUR_CLIENT_ID")

# === LINE & OpenAI ===
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

# === Firebase ===
firebase_admin.initialize_app(credentials.Certificate(json.loads(FIREBASE_CREDENTIALS)))
db = firestore.client()

# === Sessions ===
user_sessions = {}
user_message_counts = {}
story_summaries = {}
story_titles = {}
story_image_prompts = {}
story_image_urls = {}
story_current_paragraph = {}
story_paragraphs = {}
illustration_mode = {}
practice_mode = {}

# === 黑名單用戶列表 ===
BLOCKED_USER_IDS = {
    "U8a43896832cd20319724feab60c5e8cf",
}

# === 歡迎引導文字 ===
WELCOME_MESSAGE = (
    "哈囉～很高興認識你！\n"
    "我是小繪，很開心能陪你一起創作故事和插圖～\n\n"
    "你可以先試試以下指令來認識我：\n"
    "👉 問我「你是誰」\n"
    "👉 說「幫我畫一隻小狗」\n"
    "👉 之後也可以說「一起來講故事吧」開始真正的故事創作喔！\n"
    "準備好了就告訴我吧～"
)

# === Base System Prompt ===
base_system_prompt = """
你是「小繪」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助一位 50 歲以上的長輩創作 5 段故事繪本。
請用簡潔、好讀的語氣回應，每則訊息盡量不超過 35 字並適當分段。

第一階段：故事創作引導，請以「如果我有一個超能力」為主題，**只能用問題或鼓勵語句引導使用者一步步描述主角、能力、場景、事件等，不能自己創作故事內容，也不能直接給出故事開頭或細節。**

不要主導故事，保持引導與陪伴。

第二階段：插圖引導，幫助使用者描述畫面，生成的插圖上不要有故事的文字，並在完成後詢問是否需調整。

請自稱「小繪」，以朋友般的語氣陪伴使用者完成創作。
""".strip()

# === 工具函數 ===
def reset_story_memory(user_id):
    user_sessions[user_id] = {"messages": []}
    user_message_counts[user_id] = 0
    story_summaries[user_id] = ""
    story_titles[user_id] = ""
    story_image_prompts[user_id] = ""
    story_image_urls[user_id] = {}
    story_current_paragraph[user_id] = 0
    story_paragraphs[user_id] = []
    illustration_mode[user_id] = False
    practice_mode[user_id] = True
    print(f"✅ 已重置使用者 {user_id} 的故事記憶 (practice mode ON)")

# === 主頁 Route ===
@app.route("/")
def index():
    return "LINE GPT Webhook is running!"

# === LINE Follow（加好友）事件 ===
@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    print(f"👋 使用者 {user_id} 加了好友")
    reset_story_memory(user_id)
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=WELCOME_MESSAGE)
    )

# === LINE Callback ===
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
        events = json.loads(body).get("events", [])
        for event in events:
            if event.get("type") == "message":
                user_id = event["source"]["userId"]
                if user_id not in user_sessions:
                    reset_story_memory(user_id)
                    print(f"👋 使用者 {user_id} 第一次互動，自動進入練習模式")
                    # 直接主動推送「歡迎引導」訊息
                    line_bot_api.push_message(
                        user_id,
                        TextSendMessage(text=WELCOME_MESSAGE)
                    )
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    reply_token = event.reply_token
    print(f"📩 收到使用者 {user_id} 的訊息：{user_text}")

    try:
        # 處理故事總結的邏輯
        if re.search(r"(幫我統整|整理|總結|歸納|目前的故事)", user_text):
            if user_id not in user_sessions or not user_sessions[user_id]["messages"]:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="目前還沒有故事內容可以總結喔！"))
                return

            # 嘗試生成故事摘要，並進行錯誤處理
            summary = generate_story_summary(user_sessions[user_id]["messages"][-70:])
            if not summary:
                raise ValueError("生成故事摘要失敗，摘要為空")

            print(f"Generated Summary: {summary}")  # 日誌

            # 嘗試提取故事段落
            story_paragraphs[user_id] = extract_story_paragraphs(summary)
            if not story_paragraphs[user_id]:
                raise ValueError("提取故事段落失敗，段落為空")

            print(f"Story Paragraphs: {story_paragraphs[user_id]}")  # 日誌

            # 重新加上段落編號並整理
            numbered_paragraphs = "\n".join([f"{i+1}. {p}" for i, p in enumerate(story_paragraphs[user_id])])
            formatted_summary = (
                f"以下是目前整理好的五段故事內容：\n\n{numbered_paragraphs}\n\n"
                "故事已經完成了，我們可以開始畫插圖了喔！\n"
                "告訴我你想先畫哪一段，或直接說『幫我畫第一段故事的插圖』也可以～"
            )

            line_bot_api.reply_message(reply_token, TextSendMessage(text=formatted_summary))
            save_to_firebase(user_id, "user", user_text)
            save_to_firebase(user_id, "assistant", formatted_summary)
            return
        # 其他邏輯...
    except Exception as e:
        print(f"❌ 故事整理出現錯誤：{e}")
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="整理故事時發生錯誤，請稍後再試"))
        return

def generate_story_summary(messages):
    try:
        # 假設這是您生成摘要的邏輯
        if not messages:
            raise ValueError("消息內容為空，無法生成摘要")
        
        # 這裡的邏輯應該是根據消息生成摘要
        summary = "這是生成的故事摘要..."  # 假設這是摘要結果

        if not summary:
            raise ValueError("生成的摘要內容為空")

        return summary
    except Exception as e:
        print(f"❌ 生成故事摘要時發生錯誤：{e}")
        return None

def extract_story_paragraphs(summary):
    try:
        # 假設這是您從摘要提取段落的邏輯
        if not summary:
            raise ValueError("摘要為空，無法提取段落")

        # 根據摘要提取故事段落（假設這裡是將摘要分成5段）
        paragraphs = summary.split("。")  # 假設以句號分割

        if len(paragraphs) < 5:
            raise ValueError("提取的段落數量不足")

        return paragraphs[:5]  # 只返回前五段
    except Exception as e:
        print(f"❌ 提取故事段落時發生錯誤：{e}")
        return []

# --- 圖片生成部分不變 ---
def generate_dalle_image(prompt, user_id):
    try:
        print(f"🖍️ 產生圖片中：{prompt}")
        enhanced_prompt = f"""
{prompt}
No text, no words, no letters, no captions, no numbers, no Chinese or English characters, no signage, no handwriting, 
no subtitles, no labels, no written language, no symbols, no logos, no watermark, only illustration. 
請不要在圖片中加入任何文字、標題、數字、標誌、字幕、說明、書名、描述、手寫字、符號或水印，只要純粹的插畫畫面。
""".strip()
        
        response = openai.Image.create(
            model="dall-e-3",
            prompt=enhanced_prompt,
            size="1024x1024",
            response_format="url"
        )
        image_url = response['data'][0]['url']
        print(f"✅ 產生圖片成功：{image_url}")

        if user_id not in story_image_urls:
            story_image_urls[user_id] = {}
        if prompt not in story_image_urls[user_id]:
            story_image_urls[user_id][prompt] = []
        story_image_urls[user_id][prompt].append(image_url)

        try:
            print("⬇️ 開始下載圖片...")
            img_data = requests.get(image_url).content
            print("✅ 圖片下載完成")
            print("💾 開始上傳到 Imgur...")
            img_base64 = base64.b64encode(img_data).decode('utf-8')
            url = "https://api.imgur.com/3/image"
            headers = {
                "Authorization": f"Client-ID {IMGUR_CLIENT_ID}"
            }
            data = {
                "image": img_base64,
                "type": "base64",
                "privacy": "hidden"
            }
            response = requests.post(url, headers=headers, data=data)
            response_data = response.json()
            if response.status_code == 200 and response_data['success']:
                imgur_url = response_data['data']['link']
                deletehash = response_data['data']['deletehash']
                print(f"✅ 圖片已上傳到 Imgur：{imgur_url}")
                user_doc_ref = db.collection("users").document(user_id)
                user_doc_ref.collection("images").add({
                    "url": imgur_url,
                    "deletehash": deletehash,
                    "prompt": prompt,
                    "timestamp": firestore.SERVER_TIMESTAMP
                })
                print("✅ 圖片資訊已儲存到 Firestore")
                return imgur_url
            else:
                print(f"❌ Imgur API 回應錯誤：{response_data}")
                return image_url
        except Exception as e:
            print(f"❌ 上傳圖片到 Imgur 失敗：{e}")
            traceback.print_exc()
            return image_url
    except Exception as e:
        print("❌ 產生圖片失敗：", e)
        traceback.print_exc()
        return None

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
