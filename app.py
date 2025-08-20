import os, sys, json, re, time, uuid, random, traceback, threading
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import requests
import logging

# =============== 日誌設定 ===============
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(asctime)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    force=True,
)
log = logging.getLogger("app")
sys.stdout.reconfigure(encoding="utf-8")

# =============== 基礎設定 ===============
app = Flask(__name__)
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "storybotimage")
IMAGE_SIZE_ENV = (os.environ.get("IMAGE_SIZE") or "1024x1024").strip()

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    log.error("LINE credentials missing.")
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY is empty; image generation will fail.")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
log.info("🚀 app boot: public GCS URL mode (Uniform access + bucket public)")

# =============== Firebase / Firestore（容錯） ===============
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import storage as gcs_storage
from google.api_core.exceptions import GoogleAPIError

FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")

def _init_firebase():
    try:
        if firebase_admin._apps:
            return firestore.client()
        cred = None
        if FIREBASE_CREDENTIALS:
            try:
                cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
                log.info("✅ Firebase: using inline service account JSON")
            except Exception as e:
                log.warning("⚠️ FIREBASE_CREDENTIALS invalid: %s", e)
        if cred is None:
            cred = credentials.ApplicationDefault()
            log.info("✅ Firebase: using Application Default Credentials")
        firebase_admin.initialize_app(cred, {'projectId': FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None)
        return firestore.client()
    except Exception as e:
        log.error("❌ Firebase init failed: %s", e)
        return None

db = _init_firebase()

# =============== GCS（Uniform + 公開讀取） ===============
gcs_client = gcs_storage.Client()
gcs_bucket = gcs_client.bucket(GCS_BUCKET)

def gcs_upload_bytes(data: bytes, filename: str, content_type: str = "image/png"):
    t0 = time.time()
    try:
        blob = gcs_bucket.blob(filename)
        blob.cache_control = "public, max-age=31536000"
        blob.upload_from_string(data, content_type=content_type)
        url = f"https://storage.googleapis.com/{gcs_bucket.name}/{filename}"
        log.info("☁️ GCS upload ok | ms=%d | name=%s | bytes=%d | url=%s",
                 int((time.time()-t0)*1000), filename, len(data or b""), url)
        return url
    except GoogleAPIError as e:
        log.exception("❌ GCS API error: %s", e)
    except Exception as e:
        log.exception("❌ GCS unknown error: %s", e)
    return None

# =============== OpenAI 初始化 ===============
_openai_mode = None
_oai_client = None

def _init_openai():
    global _openai_mode, _oai_client
    try:
        from openai import OpenAI
        _oai_client = OpenAI(api_key=OPENAI_API_KEY)
        _openai_mode = "sdk1"
        log.info("✅ OpenAI init: sdk1")
    except Exception:
        import openai
        openai.api_key = OPENAI_API_KEY
        _oai_client = openai
        _openai_mode = "legacy"
        log.info("✅ OpenAI init: legacy")

_init_openai()

ALLOWED_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}

def _normalize_size(size: str) -> str:
    size = (size or "").strip()
    if size not in ALLOWED_SIZES:
        log.warning("⚠️ IMAGE_SIZE=%s not supported; fallback -> 1024x1024", size)
        return "1024x1024"
    return size

def openai_images_generate(prompt: str, size: str):
    size = _normalize_size(size)
    try:
        t0 = time.time()
        log.info("🖼️ images.generate start | size=%s | prompt_len=%d", size, len(prompt))
        img_bytes = None

        if _openai_mode == "sdk1":
            resp = _oai_client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size=size,
            )
            datum = resp.data[0]
            b64 = getattr(datum, "b64_json", None)
            if b64:
                import base64
                img_bytes = base64.b64decode(b64)
            elif getattr(datum, "url", None):
                r = requests.get(datum.url, timeout=120)
                r.raise_for_status()
                img_bytes = r.content
        else:
            resp = _oai_client.Image.create(
                model="gpt-image-1",
                prompt=prompt,
                size=size,
            )
            d0 = resp["data"][0]
            b64 = d0.get("b64_json")
            if b64:
                import base64
                img_bytes = base64.b64decode(b64)
            elif d0.get("url"):
                r = requests.get(d0["url"], timeout=120)
                r.raise_for_status()
                img_bytes = r.content

        if not img_bytes:
            log.error("💥 images.generate: no image content in response.")
            return None

        log.info("🖼️ images.generate ok | ms=%d | bytes=%d",
                 int((time.time()-t0)*1000), len(img_bytes))
        return img_bytes
    except Exception as e:
        log.exception("💥 images.generate error: %s", e)
        return None

# --- 角色卡類別 ---
class CharacterCard:
    def __init__(self, name_hint="主角"):
        self.name = name_hint
        self.gender = None
        self.species = None
        self.features = {
            "top_color": None, "top_type": None,
            "bottom_color": None, "bottom_type": None,
            "hair_color": None, "hair_style": None,
            "eye_color": None,
            "accessory_glasses": False,
            "accessory_hat": False
        }
    
    def update(self, key, value):
        if key in self.features:
            self.features[key] = value
            return True
        return False
    
    def render_prompt(self):
        parts = []
        
        # 優先處理物種、性別與名稱
        if self.species == "human":
            if self.gender == "male":
                parts.append("a boy")
            elif self.gender == "female":
                parts.append("a girl")
            else:
                parts.append("a person")
        elif self.species:
            parts.append(f"a {self.species}")
        else:
            parts.append("a person")

        if self.name and self.name != "主角":
            parts.append(f"named {self.name}")

        if self.species == "human":
            if self.features["top_color"] and self.features["top_type"]:
                parts.append(f"wears a {self.features['top_color']} {self.features['top_type']}")
            elif self.features["top_color"]:
                parts.append(f"wears a {self.features['top_color']} top")
            
            if self.features["bottom_color"] and self.features["bottom_type"]:
                parts.append(f"wears a {self.features['bottom_color']} {self.features['bottom_type']}")
            elif self.features["bottom_color"]:
                parts.append(f"wears {self.features['bottom_color']} bottoms")
            
            hair_parts = []
            if self.features["hair_color"]:
                hair_parts.append(self.features["hair_color"])
            if self.features["hair_style"]:
                hair_parts.append(self.features["hair_style"])
            if hair_parts:
                parts.append(f"with {' '.join(hair_parts)}")
            
            if self.features["eye_color"]:
                parts.append(f"with {self.features['eye_color']} eyes")
            
        if self.features["accessory_glasses"]:
            parts.append("wears glasses")
        if self.features["accessory_hat"]:
            parts.append("wears a hat")
        
        return ", ".join(parts)


# =============== 會話記憶（含角色卡） ===============
user_sessions = {}
user_seeds    = {}

def _ensure_session(user_id):
    sess = user_sessions.setdefault(user_id, {"messages": [], "paras": [], "characters": {}, "story_id": None})
    user_seeds.setdefault(user_id, random.randint(100000, 999999))
    if sess.get("story_id") is None:
        sess["story_id"] = f"story-{int(time.time())}-{random.randint(1000,9999)}"
    return sess

def save_chat(user_id, role, text):
    if not db: return
    try:
        db.collection("users").document(user_id).collection("chat").add({
            "role": role, "text": text, "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        log.warning("⚠️ save_chat failed: %s", e)

def save_current_story(user_id, sess):
    if not db: return
    try:
        doc = {
            "story_id": sess.get("story_id"),
            "paragraphs": sess.get("paras", []),
            "characters": {k: v.__dict__ for k, v in sess.get("characters", {}).items()},
            "updated_at": firestore.SERVER_TIMESTAMP
        }
        db.collection("users").document(user_id).collection("story").document("current").set(doc)
    except Exception as e:
        log.warning("⚠️ save_current_story failed: %s", e)

def load_current_story(user_id, sess):
    if not db: return
    try:
        doc = db.collection("users").document(user_id).collection("story").document("current").get()
        if doc.exists:
            d = doc.to_dict() or {}
            sess["story_id"] = d.get("story_id") or sess.get("story_id")
            sess["paras"] = d.get("paragraphs") or sess.get("paras", [])
            
            loaded_chars = d.get("characters", {})
            for name, char_dict in loaded_chars.items():
                card = CharacterCard(name_hint=name)
                card.__dict__.update(char_dict)
                sess["characters"][name] = card
    except Exception as e:
        log.warning("⚠️ load_current_story failed: %s", e)


# 新增一個輔助函式，專門用來清理 JSON 字串
def _clean_json_string(text: str) -> str:
    # 移除前後的換行、空格以及可能的 markdown 區塊
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.endswith("```"):
        text = text[:-3]
    # 移除前後的任何額外文字，只保留最外層的 [ ] 或 { } 區塊
    start_index = text.find('[')
    if start_index == -1:
        start_index = text.find('{')
    
    if start_index != -1:
        # 從第一個 [ 或 { 開始，找到對應的結尾 ] 或 }
        brace_count = 0
        in_string = False
        end_index = -1
        for i, char in enumerate(text[start_index:]):
            if char == '"' and (i == 0 or text[start_index+i-1] != '\\'):
                in_string = not in_string
            if not in_string:
                if char == '[' or char == '{':
                    brace_count += 1
                elif char == ']' or char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_index = start_index + i
                        break
        
        if end_index != -1:
            return text[start_index:end_index + 1]
    
    return ""

# 修改 _extract_characters_from_text 函式
def _extract_characters_from_text(text: str) -> list:
    sysmsg = (
        "你是一個角色資訊提取器。請分析使用者提供的故事文字，並找出其中的主角和關鍵角色。\n"
        "對於每個角色，請盡可能提取以下資訊：\n"
        "1. **`name`** (string): 如果有名字，請提取。若無，請用 null。\n"
        "2. **`species`** (string): 判斷角色的物種，例如 'human', 'fox', 'deer' 等。若無法判斷，請用 'unknown'。\n"
        "3. **`gender`** (string): 判斷性別，例如 'male', 'female'。若無法判斷，請用 null。\n"
        "4. **`features`** (object): 找出角色的外觀特徵，例如 'hair_color', 'eye_color', 'top_color' 等。請使用英文描述。\n"
        "   - 眼睛顏色：'eye_color': 'green'\n"
        "   - 頭髮顏色：'hair_color': 'brown'\n"
        "   - 頭髮樣式：'hair_style': 'straight hair'\n"
        "   - 上衣顏色：'top_color': 'red'\n"
        "   - 帽子：'accessory_hat': true\n"
        "   - 若無該特徵，請不要在 features 中包含該鍵值。\n"
        "**請以一個 JSON 陣列的形式輸出，不要有任何多餘的文字或解釋，只需 JSON 本身。**\n"
        "例如：\n"
        "[{\"name\": \"安琪\", \"species\": \"human\", \"gender\": \"female\", \"features\": {\"hair_color\": \"brown\", \"eye_color\": \"green\"}}, {\"name\": \"可可\", \"species\": \"fox\", \"gender\": null, \"features\": {\"color\": \"white\"}}]"
    )
    raw_response_content = ""
    try:
        msgs = [{"role": "system", "content": sysmsg}, {"role": "user", "content": text}]
        if _openai_mode == "sdk1":
            resp = _oai_client.chat.completions.create(
                model="gpt-4o-mini", messages=msgs, temperature=0.2, response_format={"type": "json_object"}
            )
            raw_response_content = resp.choices[0].message.content.strip()
        else:
            resp = _oai_client.ChatCompletion.create(
                model="gpt-4o-mini", messages=msgs, temperature=0.2, response_format={"type": "json_object"}
            )
            raw_response_content = resp["choices"][0]["message"]["content"].strip()
        
        # 使用新的輔助函式來清理回覆
        cleaned_json = _clean_json_string(raw_response_content)
        
        if not cleaned_json:
            log.error("❌ _extract_characters_from_text: Failed to clean JSON from response.")
            return []
            
        log.info(f"✅ OpenAI API raw response (cleaned): {cleaned_json[:500]}")
        
        parsed_data = json.loads(cleaned_json)
        
        # 修正邏輯：如果回傳的是單一物件，將其包裝成一個列表
        if isinstance(parsed_data, dict):
            return [parsed_data]
        elif isinstance(parsed_data, list):
            return parsed_data
        else:
            log.error("❌ _extract_characters_from_text: Unexpected JSON format.")
            return []
            
    except json.decoder.JSONDecodeError as e:
        log.error("❌ _extract_characters_from_text JSON decode error: %s", e)
        log.error("❌ Raw content that caused error: %s", raw_response_content)
        return []
    except Exception as e:
        log.error("❌ _extract_characters_from_text failed: %s", e)
        log.error("❌ Traceback: %s", traceback.format_exc())
        return []

def maybe_update_character_card(sess, user_id, text):
    try:
        new_chars_data = _extract_characters_from_text(text)
        updated = False
        
        for char_data in new_chars_data:
            name = char_data.get("name")
            species = char_data.get("species")
            gender = char_data.get("gender")
            features = char_data.get("features", {})
            
            target_card = None
            if name:
                target_card = sess["characters"].get(name)
            elif species:
                # 如果沒有名字，嘗試用物種來尋找
                target_card = next((c for c in sess["characters"].values() if c.species == species), None)

            if not target_card:
                # 建立新角色卡
                new_card = CharacterCard(name_hint=name or f"角色-{uuid.uuid4().hex[:4]}")
                new_card.name = name
                new_card.species = species
                new_card.gender = gender
                new_card.features.update(features)
                sess["characters"][new_card.name or new_card.name_hint] = new_card
                updated = True
                log.info("➕ created new character: %s", new_card.name)
            else:
                # 更新現有角色卡
                if species and not target_card.species:
                    target_card.species = species
                    updated = True
                if gender and not target_card.gender:
                    target_card.gender = gender
                    updated = True
                for key, value in features.items():
                    if target_card.update(key, value):
                        updated = True
            
        if updated:
            log.info("🧬 character_cards updated | user=%s | cards=%s", user_id, json.dumps({k: v.__dict__ for k,v in sess["characters"].items()}, ensure_ascii=False))
            save_current_story(user_id, sess)
            
    except Exception as e:
        log.exception("❌ maybe_update_character_card error: %s", e)

def render_character_card_as_text(characters: dict) -> str:
    if not characters:
        return ""
    
    char_prompts = []
    sorted_chars = sorted(characters.items())
    
    for name, card in sorted_chars:
        char_prompt = card.render_prompt()
        if char_prompt:
            char_prompts.append(char_prompt)
    
    if not char_prompts:
        return ""

    joined_prompts = " and ".join(char_prompts)
    return f"{joined_prompts}. Keep character appearance consistent."


# =============== 摘要與分段 ===============
def generate_story_summary(messages):
    sysmsg = (
        "請將以下對話整理成 5 段完整故事，每段 2–3 句（約 60–120 字）。"
        "內容應自然呈現場景、角色、主要動作與關鍵物件。\n"
        "**請用編號列點方式呈現，並盡量使用角色的具體名稱，避免使用「他們」等代詞，以確保圖像生成的角色一致性。**\n"
        "格式為：\n"
        "1. XXXXX\n"
        "2. XXXXX\n"
        "3. XXXXX\n"
        "4. XXXXX\n"
        "5. XXXXX\n"
        "請不要有額外的解釋或說明。"
    )
    msgs = [{"role": "system", "content": sysmsg}] + messages
    try:
        if _openai_mode == "sdk1":
            resp = _oai_client.chat.completions.create(
                model="gpt-4o-mini", messages=msgs, temperature=0.5
            )
            return resp.choices[0].message.content.strip()
        else:
            resp = _oai_client.ChatCompletion.create(
                model="gpt-4o-mini", messages=msgs, temperature=0.5
            )
            return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("❌ OpenAI chat error: %s", e)
        return None

def extract_paragraphs(summary):
    if not summary: return []
    lines = [re.sub(r"^\d+\.?\s*", "", x.strip()) for x in summary.split("\n") if x.strip()]
    return lines[:5]

# =============== 圖像 Prompt ===============
# 🎨 畫風回歸到最初的設定，避免風格跑掉
BASE_STYLE = (
    "a vibrant digital storybook illustration, clean bold lines, "
    "a vivid color palette, and high detail. The scene should have "
    "a dreamlike, whimsical atmosphere with soft, subtle lighting. "
    "Keep character design consistent across all images. "
    "No text, letters, logos, watermarks, signage, or brand names."
)

def build_scene_prompt(scene_desc: str, char_hint: str = "", extra: str = ""):
    parts = [BASE_STYLE, f"Scene: {scene_desc}"]
    if char_hint: parts.append(char_hint)
    if extra:    parts.append(extra)
    return " ".join(parts)

# =============== Flask routes ===============
@app.route("/")
def root():
    log.info("🏥 health check")
    return "LINE GPT Webhook is running!"

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    log.info("🌐 /callback hit | sig_present=%s | len=%s", bool(sig), len(body) if body else 0)
    if not sig:
        return "OK"
    try:
        handler.handle(body, sig)
        log.info("✅ handler.handle success")
    except InvalidSignatureError:
        log.error("❌ InvalidSignatureError")
        abort(400)
    except Exception as e:
        log.exception("💥 handle error: %s", e)
        abort(500)
    return "OK"

# =============== LINE 主流程 ===============
# 預設引導性回覆 (當AI模型呼叫失敗時使用)
GUIDING_RESPONSES = [
    "太棒了！接下來故事的主角發生了什麼事呢？",
    "這個地方聽起來很特別！你能再多描述一下它長什麼樣子嗎？",
    "好想知道這個角色是誰喔！他是個什麼樣的人呢？",
    "故事的下一段會是怎麼樣的場景呢？"
]

def generate_guiding_response(messages):
    """
    使用 AI 模型生成更貼合情境的引導性回覆。
    """
    sysmsg = (
        "你是一位充滿熱情、富有想像力的說故事夥伴，你的語氣要像一位活力四射的啦啦隊，給予使用者最直接的鼓勵與讚美。\n"
        "你的任務是結合「讚美」和「引導」，讓使用者感到被肯定，並更有動力繼續說故事。\n"
        "回覆格式必須為：`[讚美語句]！[表情符號] [開放式問題]`\n"
        "範例回覆：\n"
        "『你真的很有創意！🌟 那接下來發生了什麼事呀？』\n"
        "『這個想法很不錯！👏 他現在的心情怎麼樣呢？』\n"
        "『繼續加油，你做得很棒！💪 那他們是怎麼找到寶藏的呀？』\n"
        "『哇，這個情節太有趣了！接下來要遇到什麼樣的挑戰呢？』"
    )
    # 取最近幾條對話歷史，作為模型的上下文
    context_msgs = [{"role": "system", "content": sysmsg}] + messages[-6:]
    
    try:
        if _openai_mode == "sdk1":
            resp = _oai_client.chat.completions.create(
                model="gpt-4o-mini", messages=context_msgs, temperature=0.7
            )
            return resp.choices[0].message.content.strip()
        else:
            resp = _oai_client.ChatCompletion.create(
                model="gpt-4o-mini", messages=context_msgs, temperature=0.7
            )
            return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("❌ OpenAI guiding response error: %s", e)
        return random.choice(GUIDING_RESPONSES) # 失敗時回歸通用引導

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = (event.message.text or "").strip()
    log.info("📩 LINE text | user=%s | text=%s", user_id, text)

    sess = _ensure_session(user_id)
    load_current_story(user_id, sess)
    
    reply_token = event.reply_token

    # 1. 處理特殊指令和打招呼
    if re.search(r"(hi|Hi|你好|您好|哈囉)", text, re.IGNORECASE):
        line_bot_api.reply_message(reply_token, TextSendMessage("嗨！我是小繪機器人，一個喜歡聽故事並將它畫成插圖的夥伴！很開心認識你！"))
        return
    
    if re.search(r"一起來講故事|我們來講個故事|開始說故事|說個故事|來點故事|我想寫故事", text):
        user_sessions[user_id] = {"messages": [], "paras": [], "characters": {}, "story_id": None}
        _ensure_session(user_id) # 重新初始化 session
        line_bot_api.reply_message(reply_token, TextSendMessage("太棒了！小繪已經準備好了。我們來創造一個全新的故事吧！故事的主角是誰呢？"))
        return

    # 將使用者訊息存入 session
    sess["messages"].append({"role": "user", "content": text})
    if len(sess["messages"]) > 60:
        sess["messages"] = sess["messages"][-60:]
    save_chat(user_id, "user", text)

    # 將耗時的角色卡更新任務放入背景執行緒
    # 這樣主程式就不會被阻擋，可以立刻處理後續的邏輯或回覆
    threading.Thread(target=maybe_update_character_card, args=(sess, user_id, text), daemon=True).start()

    # 2. 處理「整理」指令
    if re.search(r"(整理|總結|summary)", text):
        # 立即回覆「處理中」訊息
        line_bot_api.reply_message(reply_token, TextSendMessage("✨ 正在為你總結故事，請稍候一下喔！"))
        
        # 在背景執行緒中執行耗時的總結操作
        def _summarize_and_push():
            compact = [{"role": "user", "content": "\n".join([m["content"] for m in sess["messages"] if m["role"] == "user"][-8:])}]
            summary = generate_story_summary(compact) or "1.\n2.\n3.\n4.\n5."
            paras = extract_paragraphs(summary)
            sess["paras"] = paras
            sess["story_id"] = f"story-{int(time.time())}-{random.randint(1000,9999)}"
            save_current_story(user_id, sess)
            line_bot_api.push_message(user_id, TextSendMessage("✨ 故事總結完成！這就是我們目前的故事：\n" + summary))
            save_chat(user_id, "assistant", summary)
        
        threading.Thread(target=_summarize_and_push, daemon=True).start()
        return

    # 3. 處理「畫圖」指令
    m = re.search(r"(畫|請畫|幫我畫)第([一二三四五12345])段", text)
    if m:
        n_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                 '1': 1, '2': 2, '3': 3, '4': 4, '5': 5}
        idx = n_map[m.group(2)] - 1
        extra = re.sub(r"(畫|請畫|幫我畫)第[一二三四五12345]段", "", text).strip(" ，,。.!！")
        
        # 檢查故事內容是否存在
        if not sess.get("paras"):
            line_bot_api.reply_message(reply_token, TextSendMessage("請先說一個故事或用「整理」指令來總結內容，我才能開始畫喔！"))
            return

        line_bot_api.reply_message(reply_token, TextSendMessage(f"收到！第 {idx+1} 段的插圖開始生成，請稍候一下下喔～"))
        threading.Thread(target=_draw_and_push, args=(user_id, idx, extra), daemon=True).start()
        return

    # 4. 處理一般對話，交由 AI 模型來生成引導
    guiding_response = generate_guiding_response(sess["messages"])

    line_bot_api.reply_message(reply_token, TextSendMessage(guiding_response))
    save_chat(user_id, "assistant", guiding_response)

@handler.add(MessageEvent)
def handle_non_text(event):
    user_id = getattr(event.source, "user_id", "unknown")
    etype = type(event.message).__name__
    log.info("🧾 LINE non-text | user=%s | type=%s", user_id, etype)
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage("目前我只看得懂文字訊息喔～"))
    except Exception:
        pass

# =============== 背景生成並 push ===============
def _get_paragraphs_for_user(sess):
    return sess.get("paras") or []

def _draw_and_push(user_id, idx, extra):
    try:
        sess = _ensure_session(user_id)
        load_current_story(user_id, sess)
        log.info("🎯 [bg] draw request | user=%s | idx=%d | extra=%s | story_id=%s", user_id, idx, extra, sess.get("story_id"))

        paras = _get_paragraphs_for_user(sess)
        if not paras or idx >= len(paras):
            line_bot_api.push_message(user_id, TextSendMessage("我需要再多一點故事內容，才能開始畫喔～"))
            return

        scene = paras[idx]
        char_hint = render_character_card_as_text(sess.get("characters", {}))
        prompt = build_scene_prompt(scene_desc=scene, char_hint=char_hint, extra=extra)
        log.info("🧩 [bg] prompt head: %s", prompt[:200])

        size = _normalize_size(IMAGE_SIZE_ENV)
        img_bytes = openai_images_generate(prompt, size=size)
        if not img_bytes:
            line_bot_api.push_message(user_id, TextSendMessage("圖片生成暫時失敗了，稍後再試一次可以嗎？"))
            return

        fname = f"line_images/{user_id}-{uuid.uuid4().hex[:6]}_s{idx+1}.png"
        public_url = gcs_upload_bytes(img_bytes, fname, "image/png")
        if not public_url:
            line_bot_api.push_message(user_id, TextSendMessage("上傳圖片時出了點狀況，等等再請我重畫一次～"))
            return

        msgs = [
            TextSendMessage(f"第 {idx+1} 段的插圖完成了！"),
            ImageSendMessage(public_url, public_url),
        ]
        
        if idx + 1 < len(paras):
            next_scene_preview = paras[idx + 1]
            msgs.append(TextSendMessage(f"要不要繼續畫第 {idx+2} 段內容呢？\n下一段的故事是：\n「{next_scene_preview}」"))

        line_bot_api.push_message(user_id, msgs)
        log.info("✅ [bg] push image sent | user=%s | url=%s", user_id, public_url)

        save_chat(user_id, "assistant", f"[image]{public_url}")

    except Exception as e:
        log.exception("💥 [bg] draw fail: %s", e)
        try:
            line_bot_api.push_message(user_id, TextSendMessage("生成中遇到小狀況，等等再試一次可以嗎？"))
        except Exception:
            pass

# =============== 啟動 ===============
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
