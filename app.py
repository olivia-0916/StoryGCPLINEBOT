import openai
import sys
import os
import json
import traceback
import re
import uuid
import requests
import time
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
app = Flask(__name__)
print("✅ Flask App initialized")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")
LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")  # 新增 Leonardo API Key

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

# 這是被遺漏的變數定義，現在已經補上
base_system_prompt = """
你是「小繪」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助一位 50 歲以上的長輩創作 5 段故事繪本。
請用簡潔、好讀的語氣回應，每則訊息盡量不超過 35 字並適當分段。

第一階段：故事創作引導，請以「如果我有一個超能力」為主題，引導使用者想像一位主角、他擁有什麼超能力、他在哪裡、遇到什麼事件、解決了什麼問題，逐步發展成五段故事。
不要主導故事，保持引導與陪伴。

第二階段：繪圖引導，幫助使用者描述畫面，生成的繪圖上不要有故事的文字，並在完成後詢問是否需調整。

請自稱「小繪」，以朋友般的語氣陪伴使用者完成創作。
""".strip()

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
    """根據對話歷史生成故事總結，只回傳五段純故事內容，不要有開場白、分隔線、標題等雜訊"""
    try:
        summary_prompt = """
請將以下對話內容整理成五個段落的故事情節，每段直接是故事內容，不要加小標題、開場白、分隔線、標題、感謝語或任何說明文字。
每段約40字，請盡量保留用戶描述的細節，不要省略重要情節或角色行動。
請確保五段故事涵蓋用戶所有描述過的重要事件與細節。
每段前面加數字（1. 2. 3. 4. 5.）。
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
    """從故事摘要中提取5段故事內容，過濾開場白與非故事內容"""
    paragraphs = [p.strip() for p in summary.split('\n') if p.strip()]
    filtered = [
        p for p in paragraphs
        if not re.match(r'^(好的|以下|讓我來|整理一下|故事如下|Summary|Here is|Here are|謝謝|---|\*\*故事標題)', p)
        and not re.match(r'^\*+$', p)
        and not re.match(r'^\*\*.*\*\*$', p)
    ]
    clean_paragraphs = [re.sub(r'^\d+\.\s*', '', p) for p in filtered]
    return clean_paragraphs[:5]

def optimize_image_prompt(story_content, user_prompt=""):
    """
    用 GPT-4 將故事段落和用戶細節描述，優化成適合 DALL·E 3 的英文 prompt，並根據用戶描述自訂風格
    """
    try:
        style_map = {
            "水彩": "watercolor style, soft colors, gentle brush strokes",
            "油畫": "oil painting, thick brush strokes, canvas texture, oil paint style",
            "色鉛筆": "colored pencil drawing, hand-drawn, sketch style, colored pencils",
            "水墨": "Chinese ink wash painting, black and white, monochrome, ink brush, traditional Asian painting, ink style, no color",
            "寫實": "photorealistic, highly detailed, realistic style, lifelike, ultra-realistic",
            "現代": "modern art style, abstract, contemporary, modern design"
        }
        user_styles = []
        for zh, en in style_map.items():
            if zh in user_prompt:
                user_styles.append(en)
        style_english = ", ".join(user_styles)
        if style_english:
            style_english = f"{style_english}, {style_english}"
        detail_prompt = user_prompt
        base_instruction = (
            "Please rewrite the following story paragraph and user details into an English prompt suitable for a picture book illustration. "
            "No text, no words, no letters, no captions, no subtitles, no watermark. "
        )
        content = f"Story paragraph: {story_content}\nDetails: {detail_prompt}"
        if style_english:
            full_prompt = f"{style_english}. {content}"
        else:
            full_prompt = content
        messages = [
            {"role": "system", "content": base_instruction},
            {"role": "user", "content": full_prompt}
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

    summary_context = story_summaries.get(user_id, "")
    prompt_with_summary = base_system_prompt
    if summary_context:
        prompt_with_summary += f"\n\n【故事摘要】\n{summary_context}\n請根據以上摘要，延續創作對話內容。"

    recent_history = user_sessions[user_id]["messages"][-30:]
    messages = [{"role": "system", "content": prompt_with_summary}] + recent_history

    try:
        print(f"📦 傳給 OpenAI 的訊息：{json.dumps(messages, ensure_ascii=False)}")
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
        )
        raw_reply = response.choices[0].message["content"]
        assistant_reply = format_reply(raw_reply)

        if encouragement_suffix:
            assistant_reply += f"\n\n{encouragement_suffix}"

        user_sessions[user_id]["messages"].append({"role": "assistant", "content": assistant_reply})

        return assistant_reply

    except Exception as e:
        print("❌ OpenAI 回應錯誤：", e)
        traceback.print_exc()
        return None

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

# === 新增 Leonardo.Ai 圖片生成函式（除錯版本） ===
def generate_leonardo_image(user_id, prompt, reference_image_url=None):
    """
    呼叫 Leonardo.Ai API 生成圖片，並可使用參考圖。
    """
    try:
        if not LEONARDO_API_KEY:
            print("❌ LEONARDO_API_KEY 環境變數未設定")
            return None
        
        # ⚠️ 除錯步驟：確認 API Key 讀取正確
        print(f"DEBUG: 讀取到的 API Key 長度為: {len(LEONARDO_API_KEY)}")

        # Leonardo.Ai 的生成 API endpoint
        api_url = "https://cloud.leonardo.ai/api/v1/generations"
        headers = {
            "Authorization": f"Bearer {LEONARDO_API_KEY}",
            "Content-Type": "application/json"
        }

        # ⚠️ 除錯步驟：使用一個簡單的、固定的 prompt
        test_prompt = "A simple, beautiful watercolor illustration of a cat on a windowsill. No text, no words, no letters."

        payload = {
            "prompt": test_prompt, # 替換為除錯用的固定 prompt
            "modelId": "6bef9f1b-29cb-40c8-b9d5-341ac2e02ad6", 
            "height": 768,
            "width": 768,
            "num_images": 1,
            "promptMagic": True,
            "promptMagicVersion": "v2",
            "negative_prompt": "text, words, captions, watermark, signature",
            "seed": -1,
            "num_inference_steps": 30
        }

        # 如果有參考圖，就加入參考圖的參數
        if reference_image_url:
            payload["init_generation_image_url"] = reference_image_url
            payload["init_generation_strength"] = 0.6 
            print(f"🔗 正在使用參考圖片: {reference_image_url}")

        print(f"🎨 呼叫 Leonardo.Ai API 產生圖片中，prompt: {test_prompt}")
        
        response = requests.post(api_url, headers=headers, json=payload)
        
        # 如果失敗，將錯誤資訊印出來
        if not response.ok:
            print(f"❌ API 請求失敗，狀態碼: {response.status_code}")
            print(f"❌ 錯誤訊息: {response.text}")
        
        response.raise_for_status()

        # ... (後續程式碼不變)
        data = response.json()
        generation_id = data['sdGenerationJob']['generationId']
        print(f"✅ 生成任務 ID: {generation_id}")

        image_url = wait_for_leonardo_image(generation_id)
        if image_url:
            print(f"✅ 圖片生成成功，URL: {image_url}")
            return upload_to_gcs_from_url(image_url, user_id, prompt) # 這裡依然用原本的 prompt 儲存
        else:
            print("❌ 圖片生成逾時或失敗")
            return None

    except Exception as e:
        print(f"❌ Leonardo.Ai 圖片生成失敗: {e}")
        traceback.print_exc()
        return None

def wait_for_leonardo_image(generation_id, timeout=120):
    """
    輪詢 Leonardo.Ai API，等待圖片生成完成並返回 URL。
    """
    start_time = time.time()
    api_url = f"https://cloud.leonardo.ai/api/v1/generations/{generation_id}"
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}"
    }

    while time.time() - start_time < timeout:
        time.sleep(5)
        try:
            response = requests.get(api_url, headers=headers)
            response.raise_for_status()
            data = response.json()

            if 'generations_v2' in data and data['generations_v2']:
                status = data['generations_v2'][0]['status']
                if status == 'COMPLETE':
                    image_url = data['generations_v2'][0]['generated_images'][0]['url']
                    return image_url
                elif status == 'FAILED':
                    print("❌ Leonardo.Ai 生成任務失敗")
                    return None
            else:
                print("⚠️ 輪詢中... 任務尚未開始或找不到資料")
        except requests.exceptions.RequestException as e:
            print(f"❌ 輪詢 Leonardo API 失敗: {e}")
            return None
    
    return None

def upload_to_gcs_from_url(image_url, user_id, prompt):
    """從 URL 下載圖片並上傳到 GCS，並保存記錄到 Firestore"""
    try:
        img_response = requests.get(image_url)
        img_response.raise_for_status()
        img_data = img_response.content
        filename = f"{user_id}_{uuid.uuid4().hex}.png"
        blob = bucket.blob(filename)
        blob.upload_from_string(img_data, content_type="image/png")
        gcs_url = f"https://storage.googleapis.com/{bucket_name}/{filename}"

        db.collection("users").document(user_id).collection("images").add({
            "url": gcs_url,
            "prompt": prompt,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
        print(f"✅ 圖片已上傳至 GCS 並儲存：{gcs_url}")
        return gcs_url
    except Exception as e:
        print(f"❌ 上傳圖片到 GCS 或儲存記錄失敗：{e}")
        traceback.print_exc()
        return None

# === 主訊息處理函式 ===
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    reply_token = event.reply_token
    print(f"📩 收到使用者 {user_id} 的訊息：{user_text}")

    try:
        if re.search(r"(開始說故事|說故事|講個故事|說一個故事|講一個故事|一起來講故事吧|我們來講故事吧)", user_text):
            reset_story_memory(user_id)
            user_sessions[user_id]["story_mode"] = True
            line_bot_api.reply_message(reply_token, TextSendMessage(
                text="太好了，我們開始講故事囉！主題是「如果我有一個超能力」，你想到的是哪一種超能力呢？"
            ))
            return

        # 在故事模式下，檢查是否需要產生第一張主角圖
        if user_sessions.get(user_id, {}).get("story_mode", False) and 'reference_image_url' not in user_sessions[user_id]:
            # 假設在第 3 則訊息時，使用者已經描述了主角，此時可以生成第一張主角圖
            # 你可以根據你的流程調整觸發時機
            if user_message_counts.get(user_id, 0) >= 3:
                # 重新生成故事摘要以取得完整主角描述
                messages = user_sessions.get(user_id, {}).get("messages", [])
                summary = generate_story_summary(messages)
                
                if summary:
                    story_paragraphs[user_id] = extract_story_paragraphs(summary)
                    story_summaries[user_id] = summary
                    # 使用第一段故事內容作為初始 prompt
                    first_paragraph_prompt = story_paragraphs[user_id][0]
                    optimized_prompt = optimize_image_prompt(first_paragraph_prompt, "water color illustration style")
                    
                    if optimized_prompt:
                        image_url = generate_leonardo_image(user_id, optimized_prompt)
                        if image_url:
                            user_sessions[user_id]['reference_image_url'] = image_url
                            reply_messages = [
                                TextSendMessage(text="太棒了！這是故事主角的第一張圖，之後的插圖都會是這個風格和主角喔："),
                                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url),
                                TextSendMessage(text="你喜歡這張圖嗎？我們可以繼續說故事，或是你也可以隨時說『幫我畫第N段故事的圖』來生成下一張插圖。")
                            ]
                            line_bot_api.reply_message(reply_token, reply_messages)
                            save_to_firebase(user_id, "user", user_text)
                            for msg in reply_messages:
                                if isinstance(msg, TextSendMessage):
                                    save_to_firebase(user_id, "assistant", msg.text)
                                elif isinstance(msg, ImageSendMessage):
                                    save_to_firebase(user_id, "assistant", f"[圖片] {msg.original_content_url}")
                            return

        # === 封面生成分支 ===
        if re.search(r"封面", user_text):
            cover_prompt = user_text.replace("幫我畫封面圖", "").replace("請畫封面", "").replace("畫封面", "").strip()
            story_title = story_titles.get(user_id, "我們的故事")
            story_summary = story_summaries.get(user_id, "")
            optimized_prompt = optimize_image_prompt(story_summary, f"封面：{cover_prompt}，故事名稱：{story_title}")
            
            if not optimized_prompt:
                optimized_prompt = f"A beautiful, colorful storybook cover illustration. Title: {story_title}. {cover_prompt}. No text, no words, no letters."
            
            reference_image_url = user_sessions.get(user_id, {}).get('reference_image_url')
            image_url = generate_leonardo_image(user_id, optimized_prompt, reference_image_url) # 傳入參考圖
            
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
            return
        
        # === 插圖生成分支 ===
        if re.search(r"(幫我畫第[一二三四五12345]段故事的圖|請畫第[一二三四五12345]段故事的插圖|畫第[一二三四五12345]段故事的圖)", user_text):
            match = re.search(r"[一二三四五12345]", user_text)
            paragraph_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '1': 1, '2': 2, '3': 3, '4': 4, '5': 5}
            paragraph_num = paragraph_map.get(match.group(0) if match else None, 1) - 1

            messages = user_sessions.get(user_id, {}).get("messages", [])
            summary = generate_story_summary(messages)
            if summary:
                story_paragraphs[user_id] = extract_story_paragraphs(summary)
                story_summaries[user_id] = summary
            
            if not story_paragraphs.get(user_id) or not (0 <= paragraph_num < len(story_paragraphs[user_id])):
                line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪還沒有整理好這段故事，請再多說一點細節吧！"))
                return
            
            story_content = story_paragraphs[user_id][paragraph_num]
            user_extra_desc = re.sub(r"(幫我畫第[一二三四五12345]段故事的圖|請畫第[一二三四五12345]段故事的插圖|畫第[一二三四五12345]段故事的圖)[，,。.!！]*", "", user_text).strip()
            
            optimized_prompt = optimize_image_prompt(story_content, user_extra_desc)
            if not optimized_prompt:
                optimized_prompt = f"A colorful, soft, watercolor-style picture book illustration for children, no text, no words, no letters. Story: {story_content} {user_extra_desc}"
            
            reference_image_url = user_sessions.get(user_id, {}).get('reference_image_url')
            image_url = generate_leonardo_image(user_id, optimized_prompt, reference_image_url) # 傳入參考圖
            
            if image_url:
                reply_messages = [
                    TextSendMessage(text=f"這是第 {paragraph_num + 1} 段故事的插圖："),
                    ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
                ]
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
        
        # === 故事標題生成分支 ===
        if re.search(r"(取故事標題|幫我取故事標題|取標題|幫我想標題)", user_text):
            story_summary = story_summaries.get(user_id, "")
            if not story_summary:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="目前還沒有故事大綱，請先完成故事內容喔！"))
                return
            
            title_prompt = f"請根據以下故事大綱，產生三個適合的故事書標題，每個不超過8字，並用1. 2. 3. 編號：\n{story_summary}"
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "你是一位擅長為故事取名的AI，請根據故事大綱產生三個簡潔有創意的故事書標題，每個不超過8字。"},
                    {"role": "user", "content": title_prompt}
                ],
                temperature=0.7,
            )
            titles = response.choices[0].message["content"].strip()
            line_bot_api.reply_message(reply_token, TextSendMessage(
                text=f"這裡有三個故事標題選項：\n{titles}\n\n請回覆你最喜歡的編號或直接輸入標題！"
            ))
            save_to_firebase(user_id, "user", user_text)
            save_to_firebase(user_id, "assistant", f"故事標題選項：\n{titles}")
            return
        
        # === 一般對話分支 ===
        encouragement_suffix = ""
        if user_sessions.get(user_id, {}).get("story_mode", False):
            encouragement_suffix = random.choice([
                "你真的很有創意！我喜歡這個設計！🌟",
                "非常好，我覺得這個想法很不錯！👏",
                "繼續加油，你做得很棒！💪",
                "你真是故事大師！😊"
            ])
        
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
