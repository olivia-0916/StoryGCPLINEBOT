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

# 定妝照（canonical portrait）
user_canonical_image_id = {}   # user_id -> image_id
user_canonical_image_url = {}  # user_id -> gcs url

# 族裔/外觀控制
user_allow_ethnicity_override = {}   # 使用者有明確指定才允許覆寫
user_signature_features = {}         # 主角招牌裝飾/著裝（英文清單字串）

# 主角名字記名
user_main_character_name = {}        # user_id -> "花媽" 等

# ========= 常數 =========
LEO_BASE = "https://cloud.leonardo.ai/api/rest/v1"
LUCID_ORIGIN_ID = "7b592283-e8a7-4c5a-9ba6-d18c31f258b9"  # Lucid Origin
IMG_W = 512
IMG_H = 512

DEFAULT_ETHNICITY_LINE = (
    "Primary ethnicity: East Asian (Han Chinese) features: black hair, dark brown eyes, warm fair skin. "
    "Maintain East Asian facial structure unless the user explicitly specifies another ethnicity or hair/eye color."
)

SAFE_STYLE_LINE = "Whimsical watercolor storybook illustration style."

SAFETY_SUFFIX = (
    " wholesome, heart-warming, strictly PG content, modest attire, no sensuality, no suggestive context, "
    "no sexualization, no fetish, safe for work."
)

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
    # 清空定妝照 & 覆寫旗標 & 名字
    user_canonical_image_id[user_id] = None
    user_canonical_image_url[user_id] = None
    user_allow_ethnicity_override[user_id] = False
    user_signature_features[user_id] = ""
    user_main_character_name[user_id] = ""
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

# --------- 內容審查安全/長度工具 ---------
def _ensure_once(text: str, needle: str) -> str:
    """確保 needle 僅附加一次。"""
    if needle.strip().lower() in (text or "").lower():
        return text
    return (text + " " + needle).strip()

def _clamp_prompt_length(text: str, max_len: int = 1450) -> str:
    """Leonardo 上限 1500，保守 1450；超過即截斷（優先保留前綴與規則）。"""
    t = re.sub(r'\s+', ' ', text or '').strip()
    if len(t) <= max_len:
        return t
    return t[:max_len]

def _sanitize_text_for_moderation(text: str) -> str:
    """統一淨化，避免兒童情境誤判與重複附加造成超長。"""
    t = text or ""

    # 敏感詞正規化
    t = re.sub(r'\b\d{1,2}\s*[-]?\s*year[-\s]?old\b', 'adult', t, flags=re.IGNORECASE)
    t = re.sub(r'(\d{1,2})\s*歲|(\d{1,2})\s*岁', '成人', t)
    t = re.sub(r'\bgirl\b', 'woman', t, flags=re.IGNORECASE)
    t = re.sub(r'\bboy\b', 'man', t, flags=re.IGNORECASE)
    t = re.sub(r'children?\s+picture[-\s]?book', 'whimsical watercolor storybook', t, flags=re.IGNORECASE)
    t = re.sub(r'\b(child|kid)\b', 'character', t, flags=re.IGNORECASE)
    t = re.sub(r'white\s+dress', 'flowing light-colored outfit', t, flags=re.IGNORECASE)

    # 正向約束
    ADULT_RULE = "depict adults only; no minors present."
    t = _ensure_once(t, ADULT_RULE)

    # 風格與安全尾註（只附加一次）
    t = _ensure_once(t, SAFE_STYLE_LINE)
    t = _ensure_once(t, SAFETY_SUFFIX)

    # 長度限制
    t = _clamp_prompt_length(t, 1450)
    return t

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
            "Please rewrite the following story paragraph and user details into an English prompt suitable for a storybook illustration in watercolor style. "
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
        p = response.choices[0].message["content"].strip()
        p = _sanitize_text_for_moderation(p)
        return _clamp_prompt_length(p, 1450)
    except Exception as e:
        print("❌ 優化插圖 prompt 失敗：", e)
        p = f"{story_content} {user_prompt}"
        p = _sanitize_text_for_moderation(p)
        return _clamp_prompt_length(p, 1450)

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

    # 拿掉不友善的那句，僅保留一個中性回覆
    low_engagement_inputs = ["不知道", "沒靈感", "嗯", "算了", "不想說", "先跳過", "跳過這題"]
    if any(phrase in user_message.strip().lower() for phrase in low_engagement_inputs):
        assistant_reply = "沒關係，我們可以慢慢想 👣"
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
def wait_for_leonardo_image(generation_id, timeout=120):
    """回傳 dict: {"url": <image_url>, "image_id": <id>}"""
    start = time.time()
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Accept": "application/json"}
    url = f"{LEO_BASE}/generations/{generation_id}"

    while time.time() - start < timeout:
        time.sleep(3)
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=False)
        if r.status_code >= 400:
            print("❌ Leonardo GET 失敗:", r.status_code, r.text[:800])
            r.raise_for_status()

        data = r.json()
        g = data.get("generations_by_pk") or {}
        status = g.get("status")
        if status == "COMPLETE":
            imgs = g.get("images") or g.get("generated_images") or []
            if imgs:
                first = imgs[0]
                return {"url": first.get("url") or first.get("image_url"),
                        "image_id": first.get("id") or first.get("imageId")}
            print("⚠️ 完成但沒有圖片資料")
            return None
        if status == "FAILED":
            print("❌ Leonardo 任務失敗")
            return None
        print("⌛ 等待中… status =", status)
    print("⏰ Leonardo 等待逾時")
    return None

def generate_leonardo_image(
    user_id,
    prompt,
    model_id=LUCID_ORIGIN_ID,
    reference_image_id=None,      # 用定妝照 image_id
    init_strength=None,           # 0.20~0.35
    use_enhance=True,
    seed=None,
    width=IMG_W,
    height=IMG_H,
    extra_negative=None           # 額外負向字串
):
    if not LEONARDO_API_KEY:
        print("❌ LEONARDO_API_KEY 未設定")
        return None

    # 基本負向詞
    base_negative = "text, letters, words, captions, subtitles, watermark, signature, different character, change hairstyle, change outfit, age change, gender change"
    if extra_negative:
        base_negative = base_negative + ", " + extra_negative

    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "storybot/1.0"
    }

    # 送出前最後一道安全處理 + 長度壓制
    safe_prompt = _sanitize_text_for_moderation(prompt)
    safe_prompt = _clamp_prompt_length(safe_prompt, 1450)

    payload = {
        "modelId": model_id,
        "prompt": safe_prompt,
        "num_images": 1,
        "width": width,
        "height": height,
        "contrast": 3.0,
        "ultra": False,
        "enhancePrompt": bool(use_enhance),
        "negative_prompt": base_negative
    }
    if seed is not None:
        payload["seed"] = int(seed)

    # 驗證 ref id（簡單 UUID）
    def _is_valid_uuid(s: str) -> bool:
        return bool(re.match(r"^[0-9a-fA-F-]{36}$", s or ""))

    use_img2img = bool(reference_image_id and init_strength is not None and _is_valid_uuid(reference_image_id))
    if use_img2img:
        payload["isInitImage"] = True
        payload["init_generation_image_id"] = reference_image_id
        payload["initStrength"] = float(init_strength)
        payload["enhancePrompt"] = False  # 降漂移

    print("🎨 Leonardo payload =>", json.dumps(payload, ensure_ascii=False))
    resp = requests.post(f"{LEO_BASE}/generations", headers=headers, json=payload,
                         timeout=45, allow_redirects=False)

    # 403 審查擋下 → 自動一次重試（更保守 + 再次淨化 + 截長）
    if resp.status_code == 403 and "Content moderated" in (resp.text or ""):
        try:
            print("🛡️ 觸發內容審查，改用更保守的安全版 prompt 重新嘗試")
            safer = safe_prompt + " extremely safe, family-friendly, suitable for all ages, absolutely no sensitive context."
            safer = _sanitize_text_for_moderation(safer)
            safer = _clamp_prompt_length(safer, 1450)
            payload["prompt"] = safer
            payload["enhancePrompt"] = False
            resp2 = requests.post(f"{LEO_BASE}/generations", headers=headers, json=payload,
                                  timeout=45, allow_redirects=False)
            if resp2.status_code >= 400:
                print("❌ 安全重試仍失敗:", resp2.status_code, resp2.text[:800])
                resp2.raise_for_status()
            gen_id = resp2.json()["sdGenerationJob"]["generationId"]
            print("✅ 安全重試成功，Generation ID:", gen_id)
            return wait_for_leonardo_image(gen_id)
        except Exception as e:
            print("❌ 安全重試例外：", e)
            return None

    if resp.status_code >= 400:
        try:
            print("❌ Leonardo POST 失敗:", resp.status_code, resp.text[:800])
        except Exception:
            pass

        # img2img 400 → 自動降級為 text-to-image
        if use_img2img:
            print("↩️ 自動降級：改用 text-to-image 重試（保留 seed 與 prompt）")
            try:
                payload.pop("isInitImage", None)
                payload.pop("init_generation_image_id", None)
                payload.pop("initStrength", None)
                payload["enhancePrompt"] = bool(use_enhance)

                resp2 = requests.post(f"{LEO_BASE}/generations", headers=headers, json=payload,
                                      timeout=45, allow_redirects=False)
                if resp2.status_code >= 400:
                    print("❌ 降級後仍失敗:", resp2.status_code, resp2.text[:800])
                    resp2.raise_for_status()
                gen_id = resp2.json()["sdGenerationJob"]["generationId"]
                print("✅ 降級重試成功，Generation ID:", gen_id)
                return wait_for_leonardo_image(gen_id)
            except Exception as e:
                print("❌ 降級重試例外：", e)
                return None

        try:
            resp.raise_for_status()
        except Exception:
            return None

    gen_id = resp.json()["sdGenerationJob"]["generationId"]
    print("✅ Leonardo Generation ID:", gen_id)
    return wait_for_leonardo_image(gen_id)

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

# ========= 名字 / 外觀特徵 / 定妝照 / 判斷 =========
_ETHNICITY_KEYWORDS = [
    "白人", "黑人", "拉丁", "歐美", "欧美", "高加索", "Caucasian", "African", "Latino", "European",
    "金髮", "金发", "blonde", "藍眼", "蓝眼", "blue eyes", "green eyes", "紅髮", "红发", "red hair"
]

def _desc_allows_ethnicity_override(text: str) -> bool:
    t = (text or "").lower()
    for kw in _ETHNICITY_KEYWORDS:
        if kw.lower() in t:
            return True
    return False

def set_main_character_name(user_id: str, name: str):
    name = (name or "").strip().strip("，,。.!！:：;；「」『』()（）[]【】")
    if not name:
        return
    user_main_character_name[user_id] = name
    base = user_character_sheet.get(user_id, "")
    if "Primary ethnicity:" not in base and not user_allow_ethnicity_override.get(user_id, False):
        base = (DEFAULT_ETHNICITY_LINE + " ") + base
    name_line = f"The main character's name is {name}. Do not print any text or name in the image."
    if name_line not in base:
        base = (base + " " + name_line).strip()
    if SAFE_STYLE_LINE not in base:
        base = (SAFE_STYLE_LINE + " " + base).strip()
    user_character_sheet[user_id] = base
    print(f"📝 已設定主角名字：{name}")

def try_parse_and_set_name(user_id: str, text: str) -> bool:
    t = (text or "").strip()
    patterns = [
        r"(?:主角|人物|她|他)?\s*(?:叫|名字是|名字為|名字为)\s*([^\s，,。!！]{1,12})",
        r"(?:設定|设置)?\s*主角名字[:：]?\s*([^\s，,。!！]{1,12})",
        r"(?:name|Name)\s*[:：]\s*([^\s，,。!！]{1,20})"
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            name = m.group(1).strip()
            set_main_character_name(user_id, name)
            return True
    return False

def augment_character_sheet_from_user(user_id, zh_desc: str):
    if not zh_desc or not zh_desc.strip():
        return
    try:
        if _desc_allows_ethnicity_override(zh_desc):
            user_allow_ethnicity_override[user_id] = True

        prompt = (
            "把以下中文人物外觀描述轉成英文、簡潔的特徵清單，用逗號分隔，"
            "例如: 'blue shirt, flower hair clip, short black hair'. 僅輸出特徵，不要多餘說明。\n"
            f"{zh_desc}"
        )
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        features = resp.choices[0].message["content"].strip()
        user_signature_features[user_id] = features

        base = user_character_sheet.get(user_id, "")
        if "Consistent main character" not in base:
            base = ("Consistent main character across all images. Same face, hairstyle, clothing, colors, proportions. ")
        if SAFE_STYLE_LINE not in base:
            base += SAFE_STYLE_LINE + " "
        if not user_allow_ethnicity_override.get(user_id, False) and "Primary ethnicity:" not in base:
            base += DEFAULT_ETHNICITY_LINE + " "

        name = user_main_character_name.get(user_id, "")
        if name and f"The main character's name is {name}." not in base:
            base += f"The main character's name is {name}. Do not print any text or name in the image. "

        user_character_sheet[user_id] = base + f" Main character always wears/has: {features}. Only the main character has these signature items."
        if SAFETY_SUFFIX.strip().lower() not in user_character_sheet[user_id].lower():
            user_character_sheet[user_id] += " " + SAFETY_SUFFIX
        print(f"✨ 角色設定卡已更新: {user_character_sheet[user_id]}")
    except Exception as e:
        print("❌ augment_character_sheet_from_user 失敗：", e)

def regenerate_canonical_portrait(user_id, seed=None):
    if seed is None:
        seed = user_fixed_seed.get(user_id) or random.randint(100000, 999999)
        user_fixed_seed[user_id] = seed

    base = user_character_sheet.get(user_id) or ""
    if SAFE_STYLE_LINE not in base:
        base = (SAFE_STYLE_LINE + " " + base).strip()
    if not user_allow_ethnicity_override.get(user_id, False) and "Primary ethnicity:" not in base:
        base = (DEFAULT_ETHNICITY_LINE + " " + base).strip()
    name = user_main_character_name.get(user_id, "")
    if name and f"The main character's name is {name}." not in base:
        base += f" The main character's name is {name}. Do not print any text or name in the image."
    if SAFETY_SUFFIX.strip().lower() not in base.lower():
        base += " " + SAFETY_SUFFIX

    user_character_sheet[user_id] = base

    prompt = base
    result = generate_leonardo_image(
        user_id=user_id,
        prompt=prompt,
        reference_image_id=None,
        init_strength=None,
        use_enhance=True,
        seed=seed,
        width=IMG_W, height=IMG_H
    )
    if result and result.get("url"):
        gcs_url = upload_to_gcs_from_url(result["url"], user_id, "[canonical portrait]")
        if gcs_url:
            user_canonical_image_id[user_id] = result.get("image_id")
            user_canonical_image_url[user_id] = gcs_url
            print(f"✅ 已更新定妝照：id={user_canonical_image_id[user_id]}, url={gcs_url}")
            return gcs_url, user_canonical_image_id[user_id]
    return None, None

def main_character_present(user_text: str, story_content: str) -> bool:
    t = f"{user_text} {story_content}".lower()
    keywords = ["主角不在", "沒有主角", "没有主角", "不含主角", "no main character", "without the main character"]
    return not any(k in t for k in keywords)

# ========= 主處理 =========
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    reply_token = event.reply_token
    print(f"📩 收到使用者 {user_id} 的訊息：{user_text}")

    try:
        # 嘗試從輸入中抓名字
        parsed = try_parse_and_set_name(user_id, user_text)
        if parsed:
            regenerate_canonical_portrait(user_id, seed=user_fixed_seed.get(user_id))
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"主角名字已設定為「{user_main_character_name[user_id]}」，我會用定妝照鎖定喔。"))
            return

        # 快捷：重設角色
        if re.search(r"(重設角色|重置角色|reset character)", user_text):
            user_character_sheet[user_id] = ""
            user_fixed_seed[user_id] = random.randint(100000, 999999)
            user_canonical_image_id[user_id] = None
            user_canonical_image_url[user_id] = None
            user_allow_ethnicity_override[user_id] = False
            user_signature_features[user_id] = ""
            user_main_character_name[user_id] = ""
            line_bot_api.reply_message(reply_token, TextSendMessage(text="已重設角色與種子，請描述主角外觀或告訴我名字，我來建立定妝照。"))
            return

        if re.search(r"(開始說故事|說故事|講個故事|說一個故事|講一個故事|一起來講故事吧|我們來講故事吧)", user_text):
            reset_story_memory(user_id)
            user_sessions[user_id]["story_mode"] = True
            line_bot_api.reply_message(reply_token, TextSendMessage(
                text="太好了，我們開始講故事囉！主題是「如果我有一個超能力」，你想到的是哪一種超能力呢？（也可以先告訴我主角名字喔）"
            ))
            return

        # 在故事模式下，自動產生第一張主角圖（定妝照）
        if user_sessions.get(user_id, {}).get("story_mode", False) and user_canonical_image_id.get(user_id) is None:
            if user_message_counts.get(user_id, 0) >= 3:
                messages = user_sessions.get(user_id, {}).get("messages", [])
                summary = generate_story_summary(messages)
                if summary:
                    story_paragraphs[user_id] = extract_story_paragraphs(summary)
                    story_summaries[user_id] = summary
                    first_paragraph_prompt = story_paragraphs[user_id][0]
                    optimized_prompt = optimize_image_prompt(first_paragraph_prompt, "watercolor, storybook style")

                    if optimized_prompt:
                        base = "Consistent main character across all images. Same face, hairstyle, clothing, colors, proportions. "
                        base += SAFE_STYLE_LINE + " "
                        if not user_allow_ethnicity_override.get(user_id, False):
                            base += DEFAULT_ETHNICITY_LINE + " "
                        name = user_main_character_name.get(user_id, "")
                        if name:
                            base += f"The main character's name is {name}. Do not print any text or name in the image. "
                        if SAFETY_SUFFIX.strip().lower() not in base.lower():
                            base += SAFETY_SUFFIX + " "
                        user_character_sheet[user_id] = (base + optimized_prompt).strip()

                        if user_id not in user_fixed_seed:
                            user_fixed_seed[user_id] = random.randint(100000, 999999)

                        result = generate_leonardo_image(
                            user_id=user_id,
                            prompt=user_character_sheet[user_id],
                            reference_image_id=None,
                            init_strength=None,
                            use_enhance=True,
                            seed=user_fixed_seed[user_id],
                            width=IMG_W, height=IMG_H
                        )
                        if result and result.get("url"):
                            gcs_url = upload_to_gcs_from_url(result["url"], user_id, optimized_prompt)
                            if gcs_url:
                                user_canonical_image_id[user_id] = result.get("image_id")
                                user_canonical_image_url[user_id] = gcs_url
                                reply_messages = [
                                    TextSendMessage(text="這是主角的第一張圖（定妝照）："),
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

        # 封面：沿用定妝照 + 低強度 img2img
        if re.search(r"封面", user_text):
            cover_prompt_raw = user_text.replace("幫我畫封面圖", "").replace("請畫封面", "").replace("畫封面", "").strip()
            story_title = story_titles.get(user_id, "我們的故事")
            summary_for_cover = story_summaries.get(user_id, "")

            if cover_prompt_raw:
                augment_character_sheet_from_user(user_id, cover_prompt_raw)
                regenerate_canonical_portrait(user_id, seed=user_fixed_seed.get(user_id))

            optimized_prompt = optimize_image_prompt(summary_for_cover, f"cover, {cover_prompt_raw}, watercolor storybook style")
            if not optimized_prompt:
                optimized_prompt = f"storybook cover, watercolor, vibrant, central composition, no text or letters. theme: {story_title}. {cover_prompt_raw}"
                optimized_prompt = _sanitize_text_for_moderation(optimized_prompt)

            base_prefix = user_character_sheet.get(user_id, "")
            final_prompt = (base_prefix + " Cover composition. " + optimized_prompt) if base_prefix else optimized_prompt

            ref_id = user_canonical_image_id.get(user_id)
            if not ref_id:
                regenerate_canonical_portrait(user_id, seed=user_fixed_seed.get(user_id))
                ref_id = user_canonical_image_id.get(user_id)
            seed = user_fixed_seed.get(user_id)

            extra_neg = None
            if not user_allow_ethnicity_override.get(user_id, False):
                extra_neg = "blonde hair, red hair, light brown hair, blue eyes, green eyes, non-East-Asian facial features"

            result = generate_leonardo_image(
                user_id=user_id,
                prompt=final_prompt,
                reference_image_id=ref_id,
                init_strength=0.24,
                use_enhance=False,
                seed=seed,
                width=IMG_W, height=IMG_H,
                extra_negative=extra_neg
            )
            if result and result.get("url"):
                gcs_url = upload_to_gcs_from_url(result["url"], user_id, final_prompt)
                if gcs_url:
                    reply_messages = [
                        TextSendMessage(text="這是你的封面："),
                        ImageSendMessage(original_content_url=gcs_url, preview_image_url=gcs_url),
                        TextSendMessage(text="需要調整可以再描述一次喔！")
                    ]
                    line_bot_api.reply_message(reply_token, reply_messages)
                    save_to_firebase(user_id, "user", user_text)
                    for msg in reply_messages:
                        if isinstance(msg, TextSendMessage):
                            save_to_firebase(user_id, "assistant", msg.text)
                        elif isinstance(msg, ImageSendMessage):
                            save_to_firebase(user_id, "assistant", f"[圖片] {msg.original_content_url}")
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪暫時畫不出封面，換句話再描述看看 🖍️"))
            return

        # 第 N 段：沿用設定卡 + 低強度 img2img(定妝照) + 固定 seed
        if re.search(r"(幫我畫第[一二三四五12345]段故事的圖|請畫第[一二三四五12345]段故事的插圖|畫第[一二三四五12345]段故事的圖)", user_text):
            match = re.search(r"[一二三四五12345]", user_text)
            paragraph_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '1': 1, '2': 2, '3': 3, '4': 4, '5': 5}
            paragraph_num = paragraph_map.get(match.group(0) if match else None, 1) - 1

            messages = user_sessions.get(user_id, {}).get("messages", [])
            new_summary = generate_story_summary(messages)
            if new_summary:
                story_paragraphs[user_id] = extract_story_paragraphs(new_summary)
                story_summaries[user_id] = new_summary

            # 取到第 N 段（不足就簡單補上）
            def ensure_paragraph(user_id, target_idx):
                pars = story_paragraphs.get(user_id) or []
                if 0 <= target_idx < len(pars):
                    return pars[target_idx]
                context = "\n".join([f"{i+1}. {p}" for i, p in enumerate(pars)]) or "1. （目前尚無內容）"
                want_num = target_idx + 1
                prompt = (
                    "請延續以下故事，補出缺少的下一段，約40字，直接給故事內容，不要加任何說明或標題。\n"
                    f"已完成的段落：\n{context}\n"
                    f"請產生第 {want_num} 段："
                )
                try:
                    resp = openai.ChatCompletion.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.7,
                    )
                    new_para = resp.choices[0].message["content"].strip()
                    while len(pars) < want_num - 1:
                        pars.append("（過渡段落：請之後補充）")
                    pars.append(new_para)
                    story_paragraphs[user_id] = pars
                    story_summaries[user_id] = "\n".join([f"{i+1}. {p}" for i, p in enumerate(pars)])
                    return new_para
                except Exception as e:
                    print("❌ ensure_paragraph 續寫失敗：", e)
                    return None

            story_text = ensure_paragraph(user_id, paragraph_num)
            if not story_text:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="小繪還沒整理好這段，我們再多描述一點點畫面吧～"))
                return

            # 使用者這次額外外觀
            user_extra_desc = re.sub(r"(幫我畫第[一二三四五12345]段故事的圖|請畫第[一二三四五12345]段故事的插圖|畫第[一二三四五12345]段故事的圖)[，,。.!！]*", "", user_text).strip()
            if user_extra_desc:
                augment_character_sheet_from_user(user_id, user_extra_desc)
                regenerate_canonical_portrait(user_id, seed=user_fixed_seed.get(user_id))

            # 確保 seed & 角色卡
            if user_id not in user_fixed_seed:
                user_fixed_seed[user_id] = random.randint(100000, 999999)
            if not user_character_sheet.get(user_id):
                seed_prompt = optimize_image_prompt(story_text, "watercolor, storybook style")
                base = "Consistent main character across all images. Same face, hairstyle, clothing, colors, proportions. "
                base += SAFE_STYLE_LINE + " "
                if not user_allow_ethnicity_override.get(user_id, False):
                    base += DEFAULT_ETHNICITY_LINE + " "
                name = user_main_character_name.get(user_id, "")
                if name:
                    base += f"The main character's name is {name}. Do not print any text or name in the image. "
                if SAFETY_SUFFIX.strip().lower() not in base.lower():
                    base += SAFETY_SUFFIX + " "
                user_character_sheet[user_id] = base + (seed_prompt or "")

            # 主角是否出場？
            mc_present = main_character_present(user_text, story_text)
            name = user_main_character_name.get(user_id, "")

            # 優化本段 prompt，並加規則
            optimized_prompt = optimize_image_prompt(story_text, user_extra_desc or "watercolor storybook style")

            base_prefix = user_character_sheet.get(user_id, "")
            scene_rules = []
            if name:
                scene_rules.append(f"The main character is named {name}. Do not print any text or the name in the image.")
            if mc_present:
                scene_rules.append("The main character appears in this scene. Only the main character uses the signature outfit/items; other characters wear different outfits.")
            else:
                scene_rules.append("The main character does not appear in this scene. Do not include the main character. Do not transfer the main character's signature items to any other characters.")
            final_prompt = (base_prefix + " " + SAFE_STYLE_LINE + " " + " ".join(scene_rules) + " Scene description: " + optimized_prompt).strip()

            # 動態負向詞
            extra_neg = []
            if not user_allow_ethnicity_override.get(user_id, False):
                extra_neg.append("blonde hair, red hair, light brown hair, blue eyes, green eyes, non-East-Asian facial features")
            sig = user_signature_features.get(user_id, "")
            if sig:
                if mc_present:
                    extra_neg.append(f"other characters wearing: {sig}")
                else:
                    extra_neg.append(f"{sig}")
            if name and not mc_present:
                extra_neg.append(f"any depiction of {name}")
            extra_neg_str = ", ".join([s for s in extra_neg if s])

            # 以定妝照為參考（若主角出場）
            ref_id = None
            init_strength = None
            if mc_present:
                ref_id = user_canonical_image_id.get(user_id)
                if not ref_id:
                    regenerate_canonical_portrait(user_id, seed=user_fixed_seed.get(user_id))
                    ref_id = user_canonical_image_id.get(user_id)
                init_strength = 0.24
            seed = user_fixed_seed.get(user_id)

            result = generate_leonardo_image(
                user_id=user_id,
                prompt=final_prompt,
                reference_image_id=ref_id,
                init_strength=init_strength,
                use_enhance=False,
                seed=seed,
                width=IMG_W, height=IMG_H,
                extra_negative=extra_neg_str
            )
            if result and result.get("url"):
                gcs_url = upload_to_gcs_from_url(result["url"], user_id, final_prompt)
                if gcs_url:
                    reply_messages = [
                        TextSendMessage(text=f"這是第 {paragraph_num + 1} 段故事的插圖："),
                        ImageSendMessage(original_content_url=gcs_url, preview_image_url=gcs_url)
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

        # 其他一般聊天
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
