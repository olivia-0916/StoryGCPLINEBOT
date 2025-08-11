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
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
from firebase_admin import firestore, storage
import firebase_admin
from firebase_admin import credentials, firestore
import base64
import random
from google.cloud import storage

# ========= 基本設定 =========
sys.stdout.reconfigure(encoding='utf-8')
app = Flask(__name__)
print("✅ Flask App initialized")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")
LEONARDO_API_KEY = (os.environ.get("LEONARDO_API_KEY") or "").strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

def get_firebase_credentials_from_env():
    return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))

firebase_admin.initialize_app(get_firebase_credentials_from_env())
db = firestore.client()

# GCS
bucket_name = "storybotimage"
gcs_client = storage.Client()
bucket = gcs_client.bucket(bucket_name)

# ========= 狀態 =========
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

# 一致性控制
user_fixed_seed = {}        # 每位使用者固定 seed
user_character_sheet = {}   # 角色設定卡（前綴）

# ========= 常數 =========
LEO_BASE = "https://cloud.leonardo.ai/api/rest/v1"
LUCID_ORIGIN_ID = "7b592283-e8a7-4c5a-9ba6-d18c31f258b9"
IMG_W = 512
IMG_H = 512

# ========= 系統提示 =========
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

# ========= 工具 =========
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
    # 重置一致性
    user_fixed_seed[user_id] = random.randint(100000, 999999)
    user_character_sheet[user_id] = ""
    print(f"✅ 已重置使用者 {user_id} 的故事記憶與一致性設定")

def generate_story_summary(messages):
    try:
        summary_prompt = """
請將以下對話內容整理成五個段落的故事情節，每段直接是故事內容，不要加小標題、開場白、分隔線、標題、感謝語或任何說明文字。
每段約40字，請盡量保留用戶描述的細節，不要省略重要情節或角色行動。
請確保五段故事涵蓋用戶所有描述過的重要事件與細節。
每段前面加數字（1. 2. 3. 4. 5.）。
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
    try:
        style_map = {
            "水彩": "watercolor style, soft colors, gentle brush strokes",
            "油畫": "oil painting, thick brush strokes, canvas texture",
            "色鉛筆": "colored pencil drawing, hand-drawn, sketch style",
            "水墨": "ink wash painting, monochrome, ink brush",
            "寫實": "photorealistic, highly detailed, realistic",
            "現代": "modern art style, contemporary, abstract"
        }
        user_styles = [en for zh, en in style_map.items() if zh in user_prompt]
        style_english = ", ".join(user_styles)
        base_instruction = (
            "Please rewrite the following story paragraph and user details into an English prompt suitable for a children picture book illustration. "
            "No text, no words, no letters, no captions, no subtitles, no watermark."
        )
        content = f"Story paragraph: {story_content}\nDetails: {user_prompt}"
        full_prompt = f"{style_english}. {content}" if style_english else content
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": base_instruction},
                      {"role": "user", "content": full_prompt}],
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

# ========= Leonardo.Ai =========
def generate_leonardo_image(user_id, prompt, model_id=LUCID_ORIGIN_ID,
                            reference_image_url=None, init_strength=None,
                            use_enhance=True, seed=None, width=IMG_W, height=IMG_H):
    """
    - reference_image_url: 上一張當底（img2img）
    - init_strength: 0~1，越小越接近原圖；建議 0.20~0.35
    - use_enhance: 第一張 True，後續 False 降低漂移
    - seed: 固定種子
    """
    if not LEONARDO_API_KEY:
        print("❌ LEONARDO_API_KEY 未設定")
        return None

    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "storybot/1.0"
    }

    payload = {
        "modelId": model_id,
        "prompt": prompt,
        "num_images": 1,
        "width": width,
        "height": height,
        "contrast": 3.0,
        "ultra": False,
        "enhancePrompt": bool(use_enhance),
        "negative_prompt": "text, letters, words, captions, subtitles, watermark, signature, different character, change hairstyle, change outfit, age change, gender change"
    }

    if seed is not None:
        payload["seed"] = int(seed)

    if reference_image_url and init_strength is not None:
        payload["init_generation_image_url"] = reference_image_url
        payload["init_strength"] = float(init_strength)
        # 後續圖通常關閉增強避免漂移
        payload["enhancePrompt"] = False

    try:
        print("🎨 Leonardo payload =>", json.dumps(payload, ensure_ascii=False))
        resp = requests.post(f"{LEO_BASE}/generations", headers=headers, json=payload,
                             timeout=45, allow_redirects=False)
        if resp.status_code >= 400:
            print("❌ Leonardo POST 失敗:", resp.status_code, resp.text[:600])
            resp.raise_for_status()

        gen_id = resp.json()["sdGenerationJob"]["generationId"]
        print("✅ Leonardo Generation ID:", gen_id)
        return wait_for_leonardo_image(gen_id)
    except Exception as e:
        print("❌ Leonardo.Ai 圖片生成失敗:", e)
        traceback.print_exc()
        return None

def wait_for_leonardo_image(generation_id, timeout=120):
    start = time.time()
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Accept": "application/json"
    }
    url = f"{LEO_BASE}/generations/{generation_id}"

    while time.time() - start < timeout:
        time.sleep(3)
        try:
            r = requests.get(url, headers=headers, timeout=30, allow_redirects=False)
            if r.status_code >= 400:
                print("❌ Leonardo GET 失敗:", r.status_code, r.text[:600])
                r.raise_for_status()

            data = r.json()
            g = data.get("generations_by_pk") or {}
            status = g.get("status")
            if status == "COMPLETE":
                imgs = g.get("images") or g.get("generated_images") or []
                if imgs:
                    return imgs[0].get("url") or imgs[0].get("image_url")
                print("⚠️ 完成但沒有圖片 URL")
                return None
            elif status == "FAILED":
                print("❌ Leonardo 任務失敗")
                return None
            else:
                print("⌛ 等待中… status =", status)
        except Exception as e:
            print("❌ 輪詢錯誤:", e)
            return None
    print("⏰ Leonardo 等待逾時")
    return None

def upload_to_gcs_from_url(image_url, user_id, prompt):
    try:
        img_response = requests.get(image_url, timeout=45)
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

# ========= 主處理 =========
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

        # 在故事模式下，自動產生第一張主角圖（建立角色設定卡、固定 seed）
        if user_sessions.get(user_id, {}).get("story_mode", False) and 'reference_image_url' not in user_sessions[user_id]:
            if user_message_counts.get(user_id, 0) >= 3:
                messages = user_sessions.get(user_id, {}).get("messages", [])
                summary = generate_story_summary(messages)
                if summary:
                    story_paragraphs[user_id] = extract_story_paragraphs(summary)
                    story_summaries[user_id] = summary
                    first_paragraph_prompt = story_paragraphs[user_id][0]
                    optimized_prompt = optimize_image_prompt(first_paragraph_prompt, "watercolor, children picture book style")

                    if optimized_prompt:
                        # 角色設定卡（前綴）
                        user_character_sheet[user_id] = (
                            "Consistent main character across all images. "
                            "Same face, hairstyle, clothing, colors, proportions. "
                            "Watercolor children picture-book style. "
                            + optimized_prompt
                        )
                        if user_id not in user_fixed_seed:
                            user_fixed_seed[user_id] = random.randint(100000, 999999)

                        url = generate_leonardo_image(
                            user_id=user_id,
                            prompt=user_character_sheet[user_id],
                            reference_image_url=None,
                            init_strength=None,
                            use_enhance=True,
                            seed=user_fixed_seed[user_id],
                            width=IMG_W, height=IMG_H
                        )
                        if url:
                            gcs_url = upload_to_gcs_from_url(url, user_id, optimized_prompt)
                            if gcs_url:
                                user_sessions[user_id]['reference_image_url'] = gcs_url
                                reply_messages = [
                                    TextSendMessage(text="這是主角的第一張圖："),
                                    ImageSendMessage(original_content_url=gcs_url, preview_image_url=gcs_url),
                                    TextSendMessage(text="喜歡嗎？說「幫我畫第N段故事的圖」可以繼續～")
                                ]
                                line_bot_api.reply_message(reply_token, reply_messages)
                                save_to_firebase(user_id, "user", user_text)
                                for msg in reply_messages:
                                    if isinstance(msg, TextSendMessage):
                                        save_to_firebase(user_id, "assistant", msg.text)
                                    elif isinstance(msg, ImageSendMessage):
                                        save_to_firebase(user_id, "assistant", f"[圖片] {msg.original_content_url}")
                                return
