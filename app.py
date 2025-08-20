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
    def __init__(self, name="無名氏"):
        self.name = name
        self.features = {}
    
    def update(self, key, value):
        if value:
            self.features[key] = value
            return True
        return False
        
    def render_prompt(self):
        parts = []
        
        # 處理名稱與角色種類
        species = self.features.get("species")
        if species:
            if "color" in self.features and species in ["fox", "deer", "cat", "dog"]:
                # 特殊處理動物顏色，強化描述
                parts.append(f"a {self.features['color']} {species} named {self.name}")
            else:
                parts.append(f"a {species} named {self.name}")
        elif self.name:
            parts.append(f"{self.name}")
        
        # 處理性別
        gender = self.features.get("gender")
        if gender:
            if gender == "男":
                parts.append("a boy")
            elif gender == "女":
                parts.append("a girl")
                
        # 處理外觀特徵
        hair_color = self.features.get("hair_color")
        hair_style = self.features.get("hair_style")
        if hair_color or hair_style:
            hair_desc = ""
            if hair_color:
                hair_desc += hair_color + " "
            if hair_style:
                hair_desc += hair_style
            if hair_desc:
                parts.append(f"with {hair_desc.strip()} hair")
        
        # 處理服裝
        top_color = self.features.get("top_color")
        top_type = self.features.get("top_type")
        if top_color and top_type:
            parts.append(f"wears a {top_color} {top_type}")
        elif top_color:
            parts.append(f"wears a {top_color} top")
            
        bottom_color = self.features.get("bottom_color")
        bottom_type = self.features.get("bottom_type")
        if bottom_color and bottom_type:
            parts.append(f"wears {bottom_color} {bottom_type}")
        elif bottom_color:
            parts.append(f"wears {bottom_color} bottoms")

        # 處理配件
        if self.features.get("accessory_glasses"):
            parts.append("wears glasses")
        if self.features.get("accessory_hat"):
            parts.append("wears a hat")
        
        # 其他特徵
        extra_features = self.features.get("extra_features")
        if extra_features:
            parts.append(extra_features)
        
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
        doc_ref = db.collection("users").document(user_id).collection("chat").document()
        doc_ref.set({
            "role": role, "text": text, "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        log.warning("⚠️ save_chat failed: %s", e)

def save_current_story(user_id, sess):
    if not db: return
    try:
        char_data = {k: v.__dict__ for k, v in sess.get("characters", {}).items()}
        
        doc = {
            "story_id": sess.get("story_id"),
            "paragraphs": sess.get("paras", []),
            "characters": char_data,
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
                card = CharacterCard(name=name)
                card.__dict__.update(char_dict)
                sess["characters"][name] = card
    except Exception as e:
        log.warning("⚠️ load_current_story failed: %s", e)


def maybe_update_character_card(sess, user_id, text):
    """
    使用LLM來動態識別角色及其特徵，並更新角色卡。
    """
    if not _oai_client:
        return
    
    sysmsg = f"""
    你是一個故事角色分析機器人。你的任務是從用戶的句子中識別新的角色或現有角色的新特徵。
    
    分析步驟：
    1. 識別句子中是否提到了**明確的角色名稱**（例如：小明、小狗、一隻貓）。名稱可以是人名、動物名或任何具體稱謂。
    2. 提取與該角色相關的**外觀特徵**（如：髮色、髮型、衣服顏色、穿著、配件等）和**物種**（例如：男孩、女孩、狗、貓、機器人）。
    3. 請將分析結果以**JSON 列表**格式輸出，不要有任何額外的文字或解釋。列表中每個元素代表一個角色。
    4. 每個 JSON 物件必須包含 `name` 和 `features` 欄位。
       - `name` 欄位必須是從句子中提取的具體名稱。
       - `features` 字典中的 key 應為英文，value 為英文或簡潔中文。
       - 範例：`[{{ "name": "小明", "features": {{ "species": "boy", "hair_color": "black" }} }}, {{ "name": "可可", "features": {{ "species": "fox", "color": "white" }} }}]`。
    
    用戶輸入：{text}
    """
    
    try:
        t0 = time.time()
        
        if _openai_mode == "sdk1":
            resp = _oai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": sysmsg}],
                temperature=0.3,
            )
            result_text = resp.choices[0].message.content.strip()
        else:
            resp = _oai_client.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": sysmsg}],
                temperature=0.3,
            )
            result_text = resp["choices"][0]["message"]["content"].strip()
            
        # 嘗試解析 JSON
        try:
            json_data = json.loads(result_text)
            
            if not isinstance(json_data, list):
                # 如果不是列表，把它包裝成列表以便統一處理
                json_data = [json_data]
            
            for char_obj in json_data:
                char_name = char_obj.get("name")
                features = char_obj.get("features", {})
                
                if not char_name:
                    log.warning("❌ LLM output did not contain a name in a character object.")
                    continue
                
                # 檢查是否已存在該角色
                if char_name in sess["characters"]:
                    char_card = sess["characters"][char_name]
                    for key, value in features.items():
                        if char_card.update(key, value):
                            log.info(f"🧬 [LLM] Updated character card | user={user_id} | name={char_name} | key={key} | value={value}")
                else:
                    # 建立新角色卡
                    new_char_card = CharacterCard(name=char_name)
                    for key, value in features.items():
                        new_char_card.update(key, value)
                    sess["characters"][char_name] = new_char_card
                    log.info(f"✨ [LLM] New character created | user={user_id} | name={char_name} | features={json.dumps(new_char_card.features, ensure_ascii=False)}")
            
            save_current_story(user_id, sess)

        except json.JSONDecodeError:
            log.warning(f"⚠️ LLM did not return valid JSON. Response: {result_text}")
        except Exception as e:
            log.error(f"💥 Failed to process LLM character extraction result: {e}")
            
    except Exception as e:
        log.error(f"❌ OpenAI character extraction failed: {e}")

def render_character_card_as_text(characters: dict) -> str:
    if not characters:
        return ""
    
    char_prompts = []
    # 確保順序固定
    sorted_chars = sorted(characters.items())
    
    for _, card in sorted_chars:
        char_prompt = card.render_prompt()
        if char_prompt:
            char_prompts.append(char_prompt)
    
    if not char_prompts:
        return ""

    joined_prompts = " and ".join(char_prompts)
    return f"{joined_prompts}. Keep character appearance consistent."

# 新增：從文字段落中提取角色名稱
def _extract_characters_from_text(text: str, all_characters: dict) -> list:
    found_chars = []
    for name in all_characters.keys():
        if name in text:
            found_chars.append(name)
    return found_chars


# =============== 摘要與分段 ===============
def generate_story_summary(messages, characters_list):
    char_names_str = "、".join(characters_list) if characters_list else "主角"
    sysmsg = (
        f"請將以下對話整理成 5 段完整故事，每段 2–3 句（約 60–120 字）。"
        f"在故事中，請**盡量使用明確的角色名稱**（例如：{char_names_str}），**不要用「他們」這類代詞**。\n"
        f"內容應自然呈現場景、角色、主要動作與關鍵物件。\n"
        f"**請用編號列點方式呈現，格式為：**\n"
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
    "No text, letters, logos, watermarks, or brand names."
)

def build_scene_prompt(scene_desc: str, char_hint: str = "", extra: str = ""):
    parts = [BASE_STYLE, f"Scene: {scene_desc}"]
    if char_hint: parts.append(char_hint)
    if extra:    parts.append(extra)
    return ", ".join(parts)

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

    # 在每次用戶發言後，嘗試更新角色卡
    threading.Thread(target=maybe_update_character_card, args=(sess, user_id, text), daemon=True).start()

    # 2. 處理「整理」指令
    if re.search(r"(整理|總結|summary)", text):
        line_bot_api.reply_message(reply_token, TextSendMessage("正在為你整理故事，請稍候一下下喔！"))
        
        # 使用線程處理耗時的總結任務
        threading.Thread(target=_summarize_and_push, args=(user_id,), daemon=True).start()
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
            line_bot_api.reply_message(reply_token, TextSendMessage("請先說一個故事或用「整理目前的故事」指令來總結內容，我才能開始畫喔！"))
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
def _summarize_and_push(user_id):
    try:
        sess = _ensure_session(user_id)
        load_current_story(user_id, sess)
        
        compact = [{"role": "user", "content": "\n".join([m["content"] for m in sess["messages"] if m["role"] == "user"][-8:])}]
        characters_list = list(sess["characters"].keys())
        summary = generate_story_summary(compact, characters_list) or "1.\n2.\n3.\n4.\n5."
        paras = extract_paragraphs(summary)
        
        sess["paras"] = paras
        sess["story_id"] = f"story-{int(time.time())}-{random.randint(1000,9999)}"
        save_current_story(user_id, sess)
        
        line_bot_api.push_message(user_id, TextSendMessage("✨ 故事總結完成！這就是我們目前的故事：\n" + summary))
        save_chat(user_id, "assistant", summary)
    except Exception as e:
        log.exception("💥 [bg] summarize fail: %s", e)
        try:
            line_bot_api.push_message(user_id, TextSendMessage("整理故事時遇到小狀況，等等再試一次可以嗎？"))
        except Exception:
            pass

def _draw_and_push(user_id, idx, extra):
    try:
        sess = _ensure_session(user_id)
        load_current_story(user_id, sess)
        log.info("🎯 [bg] draw request | user=%s | idx=%d | extra=%s | story_id=%s", user_id, idx, extra, sess.get("story_id"))

        paras = sess.get("paras") or []
        if not paras or idx >= len(paras):
            line_bot_api.push_message(user_id, TextSendMessage("我需要再多一點故事內容，才能開始畫喔～"))
            return

        scene = paras[idx]
        
        # 步驟一：從當前段落中提取角色名稱
        mentioned_char_names = _extract_characters_from_text(scene, sess.get("characters", {}))
        
        # 步驟二：根據提取到的名稱，篩選出對應的角色卡
        filtered_characters = {name: sess["characters"][name] for name in mentioned_char_names if name in sess["characters"]}
        
        # 步驟三：後台列印出用於畫圖的角色卡資訊
        log.info("🖼️ [bg] Characters for image generation: %s", json.dumps({k:v.__dict__ for k,v in filtered_characters.items()}, ensure_ascii=False))

        # 步驟四：使用篩選後的角色卡生成提示詞
        char_hint = render_character_card_as_text(filtered_characters)
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
        
        # 檢查是否有下一段故事
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
