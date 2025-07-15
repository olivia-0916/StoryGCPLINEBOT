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
from google.cloud import storage


sys.stdout.reconfigure(encoding='utf-8')
#測試是否有git
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

def get_firebase_credentials_from_env():
    return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))

firebase_admin.initialize_app(get_firebase_credentials_from_env())
db = firestore.client()

# 初始化 GCS client
bucket_name = "storybotimage"
gcs_client = storage.Client()
bucket = gcs_client.bucket(bucket_name)

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

# === 工具函數 ===
def reset_story_memory(user_id):
    user_sessions[user_id] = {"messages": [], "story_mode": False}
    user_message_counts[user_id] = 0
    story_summaries[user_id] = ""
    story_titles[user_id] = ""
    story_image_prompts[user_id] = ""
    story_image_urls[user_id] = {}
    story_current_paragraph[user_id] = 0
    story_paragraphs[user_id] = []
    illustration_mode[user_id] = False
    practice_mode[user_id] = True
    print(f"✅ 已重置使用者 {user_id} 的故事記憶")

def generate_story_summary(messages):
    """根據對話歷史生成故事總結"""
    try:
        summary_prompt = """
請將以下對話內容整理成五個段落的故事情節，每個段落用數字標記（1. 2. 3. 4. 5.）。
每段直接是故事內容，不要加小標題，每段約25字。
格式範例：
1. 小明在森林裡發現一隻受傷的小鳥。
2. 他決定帶小鳥回家照顧。
3. 經過細心照料，小鳥逐漸康復。
4. 小鳥學會了飛行，但捨不得離開。
5. 最後小鳥選擇留下來陪伴小明。

請按照以上格式整理故事內容。
"""
        messages_for_summary = [
            {"role": "system", "content": summary_prompt},
            {"role": "user", "content": "以下是故事對話內容："},
            *messages
        ]
        
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages_for_summary,
            temperature=0.7,
        )
        return response.choices[0].message["content"]
    except Exception as e:
        print("❌ 生成故事總結失敗：", e)
        return None

def extract_story_paragraphs(summary):
    """從故事摘要中提取5段故事內容"""
    paragraphs = [p.strip() for p in summary.split('\n') if p.strip()]
    # 移除段落編號
    clean_paragraphs = [re.sub(r'^\d+\.\s*', '', p) for p in paragraphs]
    return clean_paragraphs[:5]  # 確保只返回5段

def optimize_image_prompt(story_content, user_prompt=""):
    """
    用 GPT-4 將故事段落和用戶細節描述，優化成適合 DALL·E 3 的英文 prompt，並根據用戶描述自訂風格
    """
    try:
        style_keywords = [
            "水彩", "油畫", "漫畫", "素描", "像素", "插畫", "photorealistic", "pixel", "comic", "sketch", "watercolor", "oil painting"
        ]
        user_style = [kw for kw in style_keywords if kw in user_prompt]
        if user_style:
            style_instruction = "風格請依照用戶描述。"
        else:
            style_instruction = "風格要溫馨、柔和、色彩豐富。"
        base_instruction = (
            "請將以下故事段落和細節描述，改寫成適合 DALL·E 3 生成繪本風格圖片的英文 prompt，"
            f"{style_instruction} 明確要求 no text, no words, no letters, no captions, no subtitles, no watermark。"
        )
        content = f"故事段落：{story_content}\n細節描述：{user_prompt}"
        messages = [
            {"role": "system", "content": base_instruction},
            {"role": "user", "content": content}
        ]
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
        )
        return response.choices[0].message["content"].strip()
    except Exception as e:
        print("❌ 優化插圖 prompt 失敗：", e)
        return None

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    reply_token = event.reply_token
    print(f"📩 收到使用者 {user_id} 的訊息：{user_text}")

    try:
        # === 封面生成分支 ===
        if user_sessions.get(user_id, {}).get("awaiting_cover", False):
            cover_prompt = user_text.strip()
            story_title = story_titles.get(user_id, "我們的故事")
            story_summary = story_summaries.get(user_id, "")
            optimized_prompt = optimize_image_prompt(story_summary, f"封面：{cover_prompt}，故事名稱：{story_title}")
            if not optimized_prompt:
                optimized_prompt = f"A beautiful, colorful storybook cover illustration. Title: {story_title}. {cover_prompt}. No text, no words, no letters."
            image_url = generate_dalle_image(optimized_prompt, user_id)
            if image_url:
                reply_messages = [
                    TextSendMessage(text="這是你故事的封面："),
                    ImageSendMessage(original_content_url=image_url, preview_image_url=image_url),
                    TextSendMessage(text="你滿意這個封面嗎？需要調整可以再描述一次喔！")
                ]
                line_bot_api.reply_message(reply_token, reply_messages)
                save_to_firebase(user_id, "user", user_text)
                for msg in reply_messages:
                    if isinstance(msg, TextSendMessage):
                        save_to_firebase(user_id, "assistant", msg.text)
                    elif isinstance(msg, ImageSendMessage):
                        save_to_firebase(user_id, "assistant", f"[圖片] {msg.original_content_url}")
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪畫不出這個封面，試試其他描述看看 🖍️"))
            # 保持 awaiting_cover = True，直到用戶滿意
            return
        # 進入故事模式
        if re.search(r"(開始說故事|說故事|講個故事|說一個故事|講一個故事|一起來講故事吧|我們來講故事吧)", user_text):
            reset_story_memory(user_id)
            user_sessions[user_id]["story_mode"] = True
            line_bot_api.reply_message(reply_token, TextSendMessage(
                text="太好了，我們開始講故事囉！主題是「如果我有一個超能力」，你想到的是哪一種超能力呢？"
            ))
            return

        # 只在故事模式下加鼓勵語
        encouragement_suffix = ""
        if user_sessions.get(user_id, {}).get("story_mode", False):
            encouragement_suffix = random.choice([
                "你真的很有創意！我喜歡這個設計！🌟",
                "非常好，我覺得這個想法很不錯！👏",
                "繼續加油，你做得很棒！💪",
                "你真是故事大師！😊"
            ])

        # === 新增：每段插圖記錄上一張 prompt ===
        if 'last_image_prompt' not in user_sessions.get(user_id, {}):
            user_sessions.setdefault(user_id, {})['last_image_prompt'] = {}

        # === 插圖生成分支 ===
        match = re.search(r"(?:請畫|幫我畫|生成.*圖片|畫.*圖|我想要一張.*圖)(.*)", user_text)
        if match:
            prompt = match.group(1).strip()
            # 從使用者輸入中提取段落編號
            paragraph_match = re.search(r'第[一二三四五12345]段|第一段|第二段|第三段|第四段|第五段', user_text)
            if paragraph_match:
                paragraph_text = paragraph_match.group(0)
                chinese_to_number = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5}
                if paragraph_text[1] in chinese_to_number:
                    current_paragraph = chinese_to_number[paragraph_text[1]] - 1
                else:
                    current_paragraph = int(paragraph_text[1]) - 1
            else:
                current_paragraph = story_current_paragraph.get(user_id, 0)

            # 檢查段落範圍
            if current_paragraph < 0 or current_paragraph >= 5:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，故事只有五段喔！請指定1-5段之間的段落。"))
                return

            # === 新增：如果還沒摘要，先自動摘要並拆段 ===
            if (user_id not in story_paragraphs) or (not story_paragraphs[user_id]) or (len(story_paragraphs[user_id]) < 5):
                # 用目前的對話歷史自動摘要
                messages = user_sessions.get(user_id, {}).get("messages", [])
                summary = generate_story_summary(messages)
                if summary:
                    story_paragraphs[user_id] = extract_story_paragraphs(summary)
                    story_summaries[user_id] = summary
                else:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪暫時無法整理故事段落，請再試一次！"))
                    return

            # 取得該段故事內容
            story_content = ""
            if user_id in story_paragraphs and 0 <= current_paragraph < len(story_paragraphs[user_id]):
                story_content = story_paragraphs[user_id][current_paragraph]
            # === Debug log ===
            print("DEBUG story_paragraphs:", story_paragraphs.get(user_id))
            print("DEBUG current_paragraph:", current_paragraph)
            print("DEBUG story_content:", story_content)

            # === 新增：插圖細節修改 ===
            last_prompt_dict = user_sessions.setdefault(user_id, {}).setdefault('last_image_prompt', {})
            last_prompt = last_prompt_dict.get(current_paragraph, "")
            # 如果用戶只輸入細節（如「把蘑菇改成紅色」），自動組合
            if not prompt and story_content:
                prompt = story_content
            elif prompt and last_prompt:
                # 若用戶只輸入細節（不含主體），自動組合
                if len(prompt) < 20 and last_prompt:
                    prompt = f"{last_prompt}，{prompt}，其他元素維持不變"
            elif not prompt and last_prompt:
                prompt = last_prompt
            elif not prompt:
                prompt = story_content

            # 記錄本次 prompt
            last_prompt_dict[current_paragraph] = prompt

            # === 新增：自動優化 prompt ===
            optimized_prompt = optimize_image_prompt(story_content, prompt)
            if not optimized_prompt:
                # fallback
                optimized_prompt = f"A colorful, soft, watercolor-style picture book illustration for children, no text, no words, no letters. Story: {story_content} {prompt}"

            image_url = generate_dalle_image(optimized_prompt, user_id)

            if image_url:
                reply_messages = [
                    TextSendMessage(text=f"這是第 {current_paragraph + 1} 段故事的插圖："),
                    ImageSendMessage(original_content_url=image_url, preview_image_url=image_url),
                    TextSendMessage(text="你覺得這張插圖怎麼樣？需要調整嗎？")
                ]
                # 修正：只有畫第五段才結束，其他都推送下一段
                if current_paragraph == 4:
                    reply_messages.append(TextSendMessage(text="太好了！所有段落的插圖都完成了！"))
                    reply_messages.append(TextSendMessage(text="現在，請幫這個故事設計一個封面吧。你希望封面有什麼主題或元素呢？"))
                    illustration_mode[user_id] = False
                    user_sessions[user_id]["awaiting_cover"] = True  # 新增封面狀態
                else:
                    next_paragraph = current_paragraph + 1
                    if user_id in story_paragraphs and next_paragraph < len(story_paragraphs[user_id]):
                        next_story_content = story_paragraphs[user_id][next_paragraph]
                        next_story_prompt = (
                            f"要不要繼續畫第 {next_paragraph + 1} 段故事的插圖呢？\n\n"
                            f"第 {next_paragraph + 1} 段故事內容是：\n{next_story_content}\n\n"
                            "你可以跟我描述這張圖上有什麼元素，或直接說『幫我畫第"
                            f"{next_paragraph + 1}段故事的插圖』，我會根據故事內容自動生成。"
                        )
                        reply_messages.append(TextSendMessage(text=next_story_prompt))
                        story_current_paragraph[user_id] = next_paragraph

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

        # === 一般對話分支 ===
        assistant_reply = get_openai_response(user_id, user_text, encouragement_suffix)

        if not assistant_reply:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪暫時卡住了，請稍後再試喔"))
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

第一階段：故事創作引導，請以「如果我有一個超能力」為主題，引導使用者想像一位主角、他擁有什麼超能力、他在哪裡、遇到什麼事件、解決了什麼問題，逐步發展成五段故事。
不要主導故事，保持引導與陪伴。

第二階段：繪圖引導，幫助使用者描述畫面，生成的繪圖上不要有故事的文字，並在完成後詢問是否需調整。

請自稱「小繪」，以朋友般的語氣陪伴使用者完成創作。
""".strip()

def format_reply(text):
    return re.sub(r'([。！？])\s*', r'\1\n', text)

def get_openai_response(user_id, user_message, encouragement_suffix=""):
    if user_id not in user_sessions or "messages" not in user_sessions[user_id]:
        user_sessions[user_id] = {"messages": [], "story_mode": False}
    if user_id not in user_message_counts:
        user_message_counts[user_id] = 0
    if user_id not in story_summaries:
        story_summaries[user_id] = ""
    if user_id not in story_current_paragraph:
        story_current_paragraph[user_id] = 0

    # ✅ 檢查低參與輸入，回應鼓勵語
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

    # ✅ 正向語句集，避免重複與 summary 混用
    # encouragement_suffix = random.choice([
    #     "你剛剛的描述真的很棒喔 🌟",
    #     "我喜歡你用的那個比喻 👏",
    #     "慢慢來，小繪在這裡陪你 😊",
    #     "你真的很有创意！我喜欢这个设定！🌟",
    #     "非常好，我觉得这个想法很不错！👏",
    #     "继续加油，你做得很棒！💪",
    #     "你真是一个故事大师！😊"
    # ])

    recent_history = user_sessions[user_id]["messages"][-30:]
    messages = [{"role": "system", "content": prompt_with_summary}] + recent_history

    try:
        print(f"📦 傳給 OpenAI 的訊息：{json.dumps(messages, ensure_ascii=False)}")
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
        )
        raw_reply = response.choices[0].message["content"]  # 原始 GPT 回傳
        assistant_reply = format_reply(raw_reply)             # 給用戶看的格式

        # 非總結類的消息加上鼓勵語
        if encouragement_suffix:
            assistant_reply += f"\n\n{encouragement_suffix}"

        user_sessions[user_id]["messages"].append({"role": "assistant", "content": assistant_reply})

        if user_message_counts[user_id] == 30:
            summary = raw_reply  # 用原始未處理的內容
            title = extract_title_from_reply(raw_reply)
            story_summaries[user_id] = summary
            story_titles[user_id] = title
            story_image_prompts[user_id] = f"故事名稱：{title}，主題是：{summary}"
            story_paragraphs[user_id] = extract_story_paragraphs(summary)

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
        請不要在圖片中加入任何文字、標題、數字、標誌、字幕、說明、書名、描述、手寫字、符號或水印，只要純粹繪本圖片畫面。
        """.strip()
        response = openai.Image.create(
            model="dall-e-3",
            prompt=enhanced_prompt,
            size="1024x1024",
            response_format="url"
        )
        image_url = response['data'][0]['url']
        print(f"✅ 產生圖片成功：{image_url}")

        # 下載圖片
        img_data = requests.get(image_url).content

        # 產生唯一檔名
        filename = f"{user_id}_{uuid.uuid4().hex}.png"

        # 上傳到 GCS
        blob = bucket.blob(filename)
        blob.upload_from_string(img_data, content_type="image/png")
        # 不要再呼叫 blob.make_public()
        gcs_url = f"https://storage.googleapis.com/{bucket_name}/{filename}"
        print(f"✅ 圖片已上傳到 GCS：{gcs_url}")

        # 儲存圖片 URL 到 Firestore
        user_doc_ref = db.collection("users").document(user_id)
        user_doc_ref.collection("images").add({
            "url": gcs_url,
            "prompt": prompt,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
        print("✅ 圖片資訊已儲存到 Firestore")

        return gcs_url

    except Exception as e:
        print("❌ 產生圖片失敗：", e)
        traceback.print_exc()
        return None

@app.route("/story/<user_id>")
def view_story(user_id):
    try:
        # 從 Firebase 獲取使用者資料
        user_doc_ref = db.collection("users").document(user_id)
        images = user_doc_ref.collection("images").order_by("timestamp").get()
        chat = user_doc_ref.collection("chat").order_by("timestamp").get()
        
        # 整理資料
        story_data = {
            "title": story_titles.get(user_id, "我們的故事"),
            "summary": story_summaries.get(user_id, ""),
            "images": [],
            "content": []
        }
        
        # 處理圖片
        for img in images:
            story_data["images"].append({
                "url": img.get("url"),
                "prompt": img.get("prompt")
            })
            
        # 處理對話內容
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
        # 從 Firebase 獲取使用者資料
        user_doc_ref = db.collection("users").document(user_id)
        images = user_doc_ref.collection("images").order_by("timestamp").get()
        chat = user_doc_ref.collection("chat").order_by("timestamp").get()
        
        # 整理資料
        story_data = {
            "title": story_titles.get(user_id, "我們的故事"),
            "summary": story_summaries.get(user_id, ""),
            "images": [],
            "content": []
        }
        
        # 處理圖片
        for img in images:
            story_data["images"].append({
                "url": img.get("url"),
                "prompt": img.get("prompt")
            })
            
        # 處理對話內容
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
    

