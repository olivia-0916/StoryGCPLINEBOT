import openai
import sys
import os
import json
import traceback
import re
import uuid
import requests
from datetime import datetime
from flask import Flask, request, abort, render_template, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
from firebase_admin import firestore, storage
import firebase_admin
from firebase_admin import credentials, firestore
import base64
import random

sys.stdout.reconfigure(encoding='utf-8')
app = Flask(__name__)
print("✅ Flask App initialized")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")
IMGUR_CLIENT_ID = os.environ.get("IMGUR_CLIENT_ID")
IMGUR_CLIENT_SECRET = os.environ.get("IMGUR_CLIENT_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

# Firebase initialization
firebase_admin.initialize_app(credentials.Certificate(json.loads(FIREBASE_CREDENTIALS)))
db = firestore.client()

# Session variables
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

# Function to reset user session memory
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
    print(f"✅ Reset memory for user {user_id} and enabled practice mode")

# Function to generate images
def generate_dalle_image(prompt, user_id, image_url=None):
    try:
        print(f"🖍️ Generating image: {prompt}")
        enhanced_prompt = f"""
{prompt}
No text, no words, no letters, no captions, no numbers, no Chinese or English characters, no signage, no handwriting, no subtitles, no labels, no written language, no symbols, no logos, no watermark, only illustration.
請不要在圖片中加入任何文字、標題、數字、標誌、字幕、說明、書名、描述、手寫字、符號或水印，只要純粹的插畫畫面。
""".strip()

        if image_url:
            # Inpainting (editing the existing image)
            response = openai.Image.create(
                model="dall-e-3",
                prompt=enhanced_prompt,
                image_url=image_url,
                response_format="url"
            )
        else:
            # Regular image generation
            response = openai.Image.create(
                model="dall-e-3",
                prompt=enhanced_prompt,
                size="1024x1024",
                response_format="url"
            )

        image_url = response['data'][0]['url']
        print(f"✅ Image generated: {image_url}")

        if user_id not in story_image_urls:
            story_image_urls[user_id] = {}
        if prompt not in story_image_urls[user_id]:
            story_image_urls[user_id][prompt] = []
        story_image_urls[user_id][prompt].append(image_url)

        # Save to Imgur for hosting
        img_data = requests.get(image_url).content
        img_base64 = base64.b64encode(img_data).decode('utf-8')
        url = "https://api.imgur.com/3/image"
        headers = {"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"}
        data = {"image": img_base64, "type": "base64", "privacy": "hidden"}
        response = requests.post(url, headers=headers, data=data)
        response_data = response.json()

        if response.status_code == 200 and response_data['success']:
            imgur_url = response_data['data']['link']
            deletehash = response_data['data']['deletehash']
            print(f"✅ Image uploaded to Imgur: {imgur_url}")
            return imgur_url
        else:
            print(f"❌ Imgur API error: {response_data}")
            return image_url
    except Exception as e:
        print(f"❌ Image generation failed: {e}")
        traceback.print_exc()
        return None

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
                user_text = event["message"]["text"]
                reply_token = event["replyToken"]
                if user_id not in user_sessions:
                    reset_story_memory(user_id)

                # Handle image generation requests with inpainting feature (adding to existing images)
                if "加上" in user_text:  # Detect "add" command
                    image_url = story_image_urls.get(user_id, {}).get("latest_image_url")
                    if image_url:
                        prompt = f"請在這張圖片中加上 {user_text}"
                        new_image_url = generate_dalle_image(prompt, user_id, image_url)
                        if new_image_url:
                            line_bot_api.reply_message(
                                reply_token,
                                [TextSendMessage(text=f"這是添加新元素後的圖片："),
                                 ImageSendMessage(original_content_url=new_image_url, preview_image_url=new_image_url)]
                            )
                        else:
                            line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，無法處理這個要求，請再試一次。"))
                    else:
                        line_bot_api.reply_message(reply_token, TextSendMessage(text="目前沒有圖片可以加上元素，請先生成圖片。"))
                else:
                    # Handle normal message and image generation
                    if "請畫" in user_text:
                        prompt = user_text.replace("請畫", "").strip()
                        new_image_url = generate_dalle_image(prompt, user_id)
                        if new_image_url:
                            line_bot_api.reply_message(
                                reply_token,
                                [TextSendMessage(text="這是你請求的圖片："),
                                 ImageSendMessage(original_content_url=new_image_url, preview_image_url=new_image_url)]
                            )
                    else:
                        line_bot_api.reply_message(reply_token, TextSendMessage(text="請描述你想要畫的圖片！"))

    except InvalidSignatureError:
        abort(400)
    return "OK"

if __name__ == "__main__":
    app.run(debug=True)
