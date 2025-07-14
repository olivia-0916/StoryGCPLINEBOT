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
    print(f"🔍 目前 practice_mode: {practice_mode.get(user_id)}, illustration_mode: {illustration_mode.get(user_id)}")

    try:
        # --- 偵測「一起來講故事吧」或「我們來講故事吧」指令，切換到正式創作階段 ---
        if "一起來講故事吧" in user_text or "我們來講故事吧" in user_text:
            reset_story_memory(user_id)
            practice_mode[user_id] = False
            illustration_mode[user_id] = False
            story_current_paragraph[user_id] = 0
            story_paragraphs[user_id] = []
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="太好了，我們開始講故事囉！這次的主題是『如果我有一個超能力』，你會想像自己有什麼超能力呢？可以先告訴我主角的名字和能力喔！")
            )
            return

        # --- 正式創作前，優先判斷畫圖指令 ---
        if practice_mode.get(user_id, True):
            # 只要還沒進入正式創作，優先判斷畫圖指令
            if re.search(r'第[一二三四五12345]段', user_text):
                practice_mode[user_id] = False
                illustration_mode[user_id] = True
                story_current_paragraph[user_id] = 0
                line_bot_api.reply_message(reply_token, TextSendMessage(text="好的，現在進入正式故事插圖創作模式！請再說一次你想畫哪一段故事的插圖，或直接描述你想畫的內容。"))
                return
            match = re.match(r"^(請畫|幫我畫|生成.*圖片|畫.*圖|我想要一張.*圖)(.*)", user_text)
            if match:
                prompt = match.group(2).strip()
                if not prompt:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="請告訴我你想畫什麼內容喔！"))
                    return
                print(f"🔔 generate_dalle_image prompt: {prompt}")
                image_url = generate_dalle_image(prompt, user_id)
                if image_url:
                    reply_messages = [
                        TextSendMessage(text=f"這是你練習畫的圖片："),
                        ImageSendMessage(original_content_url=image_url, preview_image_url=image_url),
                        TextSendMessage(text="你覺得這張圖怎麼樣？還想再畫其他東西嗎？")
                    ]
                    line_bot_api.reply_message(reply_token, reply_messages)
                    save_to_firebase(user_id, "user", user_text)
                    for msg in reply_messages:
                        if isinstance(msg, TextSendMessage):
                            save_to_firebase(user_id, "assistant", msg.text)
                        elif isinstance(msg, ImageSendMessage):
                            save_to_firebase(user_id, "assistant", f"[圖片] {msg.original_content_url}")
                else:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，小繪畫不出這張圖喔，換個描述試試看吧～"))
                return
            # 其他情況才進入 AI 對話
            assistant_reply = get_openai_response(user_id, user_text)
            line_bot_api.reply_message(reply_token, TextSendMessage(text=assistant_reply))
            save_to_firebase(user_id, "user", user_text)
            save_to_firebase(user_id, "assistant", assistant_reply)
            return

        # 若故事段落已經集滿五段，自動進入插圖模式，推送第一段內容
        if not illustration_mode.get(user_id, False) and len(story_paragraphs.get(user_id, [])) == 5:
            illustration_mode[user_id] = True
            story_current_paragraph[user_id] = 0
            first_paragraph = story_paragraphs[user_id][0]
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(
                    text=f"故事整理完成！我們可以開始生成插圖了。\n\n第一段故事內容是：\n{first_paragraph}\n\n你可以描述這張圖有什麼元素，或直接說「幫我畫」我也會自動生成！"
                )
            )
            return

        # --- 故事總結分支 ---
        if re.search(r"(幫我統整|整理|總結|歸納|目前的故事)", user_text):
            if user_id not in user_sessions or not user_sessions[user_id]["messages"]:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="目前還沒有故事內容可以總結喔！"))
                return

            summary = generate_story_summary(user_sessions[user_id]["messages"])
            if summary:
                # 儲存故事段落
                story_paragraphs[user_id] = extract_story_paragraphs(summary)
                illustration_mode[user_id] = True
                story_current_paragraph[user_id] = 0

                # 重新加上段落編號
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
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，我現在無法總結故事，請稍後再試。"))
            return

        # --- 正式故事創作階段，插圖生成 ---
        if illustration_mode.get(user_id, False):
            match = re.match(r"^(請畫|幫我畫|生成.*圖片|畫.*圖|我想要一張.*圖)(.*)", user_text)
            if match:
                prompt = match.group(2).strip()
                # 嘗試從使用者輸入中提取段落編號（中文或數字）
                paragraph_match = re.search(r'第[一二三四五12345]段', user_text)
                if paragraph_match:
                    chinese_to_number = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5}
                    p = paragraph_match.group(0)
                    num_char = p[1]
                    if num_char in chinese_to_number:
                        current_paragraph = chinese_to_number[num_char] - 1
                    else:
                        current_paragraph = int(num_char) - 1
                    manual_select = True
                else:
                    current_paragraph = story_current_paragraph.get(user_id, 0)
                    manual_select = False

                # 檢查段落範圍
                if current_paragraph < 0 or current_paragraph >= 5:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，故事只有五段喔！請指定1-5段之間的段落。"))
                    return

                # 取得該段故事內容
                story_content = ""
                if user_id in story_paragraphs and 0 <= current_paragraph < len(story_paragraphs[user_id]):
                    story_content = story_paragraphs[user_id][current_paragraph]

                # 如果用戶描述內容為空，直接用故事內容
                if not prompt:
                    if not story_content:
                        line_bot_api.reply_message(reply_token, TextSendMessage(text="這段故事內容還沒寫好，請先補充故事再畫圖喔！"))
                        return
                    final_prompt = story_content
                else:
                    final_prompt = f"{story_content} {prompt}".strip() if story_content else prompt
                # 防呆：檢查 final_prompt 長度
                if not final_prompt or len(final_prompt.strip()) < 10:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="這段故事內容太短，請補充描述再試一次！"))
                    return
                # debug print
                print(f"[DEBUG] story_paragraphs[{user_id}]: {story_paragraphs.get(user_id)}")
                print(f"[DEBUG] current_paragraph: {current_paragraph}")
                print(f"[DEBUG] story_content: '{story_content}'")
                print(f"[DEBUG] final_prompt: '{final_prompt}'")
                try:
                    image_url = generate_dalle_image(final_prompt, user_id)
                except Exception as e:
                    print(f"❌ DALL·E API 錯誤：{e}")
                    line_bot_api.reply_message(reply_token, TextSendMessage(text=f"DALL·E API 錯誤：{e}"))
                    return

                if image_url:
                    reply_messages = [
                        TextSendMessage(text=f"這是第 {current_paragraph + 1} 段故事的插圖："),
                        ImageSendMessage(original_content_url=image_url, preview_image_url=image_url),
                        TextSendMessage(text="你覺得這張插圖怎麼樣？需要調整嗎？")
                    ]
                    # 優化：自動推送下一段故事內容與引導語
                    next_paragraph = current_paragraph + 1
                    if next_paragraph < 5 and user_id in story_paragraphs and len(story_paragraphs[user_id]) >= 5:
                        next_story_content = story_paragraphs[user_id][next_paragraph]
                        next_prompt = (
                            f"要不要繼續畫第 {next_paragraph + 1} 段故事的插圖呢？\n\n"
                            f"第 {next_paragraph + 1} 段故事內容是：\n{next_story_content}\n\n"
                            "你可以跟我描述這張圖上有什麼元素，或直接說『幫我畫第"
                            f"{next_paragraph + 1}段故事的插圖』，我會根據故事內容自動生成。"
                        )
                        reply_messages.append(TextSendMessage(text=next_prompt))
                        story_current_paragraph[user_id] = next_paragraph
                    elif next_paragraph >= 5:
                        reply_messages.append(TextSendMessage(text="太好了！所有段落的插圖都完成了！"))
                        illustration_mode[user_id] = False
                    line_bot_api.reply_message(reply_token, reply_messages)
                    save_to_firebase(user_id, "user", user_text)
                    for msg in reply_messages:
                        if isinstance(msg, TextSendMessage):
                            save_to_firebase(user_id, "assistant", msg.text)
                        elif isinstance(msg, ImageSendMessage):
                            save_to_firebase(user_id, "assistant", f"[圖片] {msg.original_content_url}")
                else:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪畫不出這張圖，試試其他描述看看 🖍️"))
                return

        # 其他狀況：一般對話
        assistant_reply = get_openai_response(user_id, user_text)
        if not assistant_reply:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪暫時卡住了，請稍後再試 🌧️"))
            return

        line_bot_api.reply_message(reply_token, TextSendMessage(text=assistant_reply))
        save_to_firebase(user_id, "user", user_text)
        save_to_firebase(user_id, "assistant", assistant_reply)

    except Exception as e:
        print("❌ 發生錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪出了一點小狀況，請稍後再試 🙇"))

def save_to_firebase(user_id, role, text):
    try:
        user_doc_ref = db.collection("users").document(user_id)
        user_doc_ref.collection("chat").add({
            "role": role,
            "text": text,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
        print(f"✅ Firebase 已儲存訊息（{role}）")
    except Exception as e:
        print(f"⚠️ 儲存 Firebase 失敗（{role}）：", e)

base_system_prompt = """
你是「小繪」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助一位 50 歲以上的長輩創作 5 段故事繪本。
請用簡潔、好讀的語氣回應，每則訊息盡量不超過 35 字並適當分段。

第一階段：故事創作引導，請以「如果我有一個超能力」為主題，**只能用問題或鼓勵語句引導使用者一步步描述主角、能力、場景、事件等，不能自己創作故事內容，也不能直接給出故事開頭或細節。**

不要主導故事，保持引導與陪伴。

第二階段：插圖引導，幫助使用者描述畫面，生成的插圖上不要有故事的文字，並在完成後詢問是否需調整。

請自稱「小繪」，以朋友般的語氣陪伴使用者完成創作。
""".strip()

def format_reply(text):
    return re.sub(r'([。！？])\s*', r'\1\n', text)

def get_openai_response(user_id, user_message):
    if user_id not in user_sessions:
        user_sessions[user_id] = {"messages": []}
    if user_id not in user_message_counts:
        user_message_counts[user_id] = 0
    if user_id not in story_summaries:
        story_summaries[user_id] = ""
    if user_id not in story_current_paragraph:
        story_current_paragraph[user_id] = 0

    low_engagement_inputs = ["不知道", "沒靈感", "嗯", "算了", "不想說", "先跳過", "跳過這題"]
    if any(phrase in user_message.strip().lower() for phrase in low_engagement_inputs):
        assistant_reply = random.choice([
            "沒關係，我們可以慢慢想 👣",
            "如果不想說，我們可以跳過喔 🙂",
            "不用急～你已經很棒了 💪"
        ])
        user_sessions[user_id]["messages"].append({"role": "user", "content": user_message})
        user_sessions[user_id]["messages"].append({"role": "assistant", "content": assistant_reply})
        return assistant_reply

    user_sessions[user_id]["messages"].append({"role": "user", "content": user_message})
    user_message_counts[user_id] += 1

    if user_message_counts[user_id] % 6 == 0:
        story_current_paragraph[user_id] = min(4, story_current_paragraph[user_id] + 1)

    if user_message_counts[user_id] == 30:
        user_sessions[user_id]["messages"].append({
            "role": "user",
            "content": "請為這三十段故事取個標題，大約五六個字就好。"
        })

    summary_context = story_summaries[user_id]
    prompt_with_summary = base_system_prompt
    if summary_context:
        prompt_with_summary += f"\n\n【故事摘要】\n{summary_context}\n請根據以上摘要，延續創作對話內容。"

    current_paragraph = story_current_paragraph.get(user_id, 0)
    prompt_with_summary += f"\n\n現在是第{current_paragraph+1}段，請一次只寫一段故事，不要一次補完全部段落，等使用者輸入下一段內容再繼續。"

    encouragement_suffix = random.choice([
        "你已經做得很好了 ❤️",
        "很棒喔～慢慢來就好 🙂",
        "不急，我會在這裡陪著你 🌿",
        "這個想法很有趣 👌",
        "慢慢想也沒關係喔～",
        "嗯嗯，我懂的 🙂",
        "可以再說說看喔～",
        "還有什麼細節想加嗎？",
        "小繪豎起耳朵在聽 👂✨",
        "噗～我覺得這樣很可愛呀 🐣",
        "小繪覺得很讚耶 👍",
        "還想補充什麼呢？",
        "我在聽，你慢慢說 🌱",
        "如果不想說也沒關係喔 🙂",
        "有任何想法都可以跟我分享 😊",
        "還有別的想法嗎？",
    ])

    recent_history = user_sessions[user_id]["messages"][-70:]
    messages = [{"role": "system", "content": prompt_with_summary}] + recent_history

    try:
        print(f"📦 傳給 OpenAI 的訊息：{json.dumps(messages, ensure_ascii=False)}")
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
        )
        assistant_reply = response.choices[0].message["content"]
        assistant_reply = format_reply(assistant_reply)

        # --- 自動加上編號 ---
        if ("整理" in user_message or "總結" in user_message or "歸納" in user_message) and not re.search(r"^1[\.．]", assistant_reply, re.MULTILINE):
            lines = [l for l in assistant_reply.strip().split('\n') if l.strip()]
            # 只 enumerate 每行都超過 10 字，且至少 3 行的內容
            if len(lines) >= 3 and all(len(l) >= 10 for l in lines):
                assistant_reply = "\n".join([f"{i+1}. {l}" for i, l in enumerate(lines)])

        if "故事名稱" not in assistant_reply and "總結" not in assistant_reply:
            assistant_reply += f"\n\n{encouragement_suffix}"

        user_sessions[user_id]["messages"].append({"role": "assistant", "content": assistant_reply})

        if user_message_counts[user_id] == 30:
            summary = extract_summary_from_reply(assistant_reply)
            title = extract_title_from_reply(assistant_reply)
            story_summaries[user_id] = summary
            story_titles[user_id] = title
            story_image_prompts[user_id] = f"故事名稱：{title}，主題是：{summary}"

        return assistant_reply

    except Exception as e:
        print("❌ OpenAI 回應錯誤：", e)
        traceback.print_exc()
        return None

def extract_summary_from_reply(reply_text):
    parts = reply_text.strip().split("\n")
    for part in reversed(parts):
        if "這段故事" in part or "總結" in part or "目前的故事內容" in part:
            return part.strip()
    return ""

def extract_title_from_reply(reply_text):
    match = re.search(r"(?:故事名稱|標題)[:：]?([\w\u4e00-\u9fff]{3,8})", reply_text)
    return match.group(1).strip() if match else "我們的故事"

def generate_dalle_image(prompt, user_id):
    try:
        print(f"🖍️ 產生圖片中：{prompt}")
        enhanced_prompt = f"""
{prompt}
No text, no words, no letters, no captions, no numbers, no Chinese or English characters, no signage, no handwriting, no subtitles, no labels, no written language, no symbols, no logos, no watermark, only illustration.
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

@app.route("/story/<user_id>")
def view_story(user_id):
    try:
        user_doc_ref = db.collection("users").document(user_id)
        images = user_doc_ref.collection("images").order_by("timestamp").get()
        chat = user_doc_ref.collection("chat").order_by("timestamp").get()
        story_data = {
            "title": story_titles.get(user_id, "我們的故事"),
            "summary": story_summaries.get(user_id, ""),
            "images": [],
            "content": []
        }
        for img in images:
            story_data["images"].append({
                "url": img.get("url"),
                "prompt": img.get("prompt")
            })
        for msg in chat:
            if msg.get("role") == "assistant":
                story_data["content"].append(msg.get("text"))
        return render_template("story.html", story=story_data)
    except Exception as e:
        print(f"❌ 讀取故事失敗：{e}")
        return "無法讀取故事", 404

@app.route("/api/story/<user_id>")
def get_story_data(user_id):
    try:
        user_doc_ref = db.collection("users").document(user_id)
        images = user_doc_ref.collection("images").order_by("timestamp").get()
        chat = user_doc_ref.collection("chat").order_by("timestamp").get()
        story_data = {
            "title": story_titles.get(user_id, "我們的故事"),
            "summary": story_summaries.get(user_id, ""),
            "images": [],
            "content": []
        }
        for img in images:
            story_data["images"].append({
                "url": img.get("url"),
                "prompt": img.get("prompt")
            })
        for msg in chat:
            if msg.get("role") == "assistant":
                story_data["content"].append(msg.get("text"))
        return jsonify(story_data)
    except Exception as e:
        print(f"❌ 讀取故事失敗：{e}")
        return jsonify({"error": "無法讀取故事"}), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
