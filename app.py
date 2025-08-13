# app.py
import os, sys, json, time, uuid, re, random, traceback, tempfile, gc, threading
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import requests

# ---------- 基礎設定 ----------
sys.stdout.reconfigure(encoding="utf-8")
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET      = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY           = os.environ.get("OPENAI_API_KEY")
LEONARDO_API_KEY         = os.environ.get("LEONARDO_API_KEY")
FIREBASE_CREDENTIALS     = os.environ.get("FIREBASE_CREDENTIALS")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

# ---------- Firebase / GCS ----------
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import storage as gcs_storage

def _firebase_creds():
    return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
firebase_admin.initialize_app(_firebase_creds())
db = firestore.client()

GCS_BUCKET = "storybotimage"
gcs_client = gcs_storage.Client()
gcs_bucket = gcs_client.bucket(GCS_BUCKET)

# ---------- Leonardo ----------
LEO_BASE  = "https://cloud.leonardo.ai/api/rest/v1"
LEO_MODEL = "7b592283-e8a7-4c5a-9ba6-d18c31f258b9"   # Lucid Origin
IMG_W = 512
IMG_H = 512

# ---------- 會話 / 記憶 ----------
user_sessions     = {}  # {uid: {"messages":[...], "summary":"", "paras":[...]} }
user_last_images  = {}  # {uid: {"url":..., "image_id":...}}
user_seeds        = {}  # {uid: int}
user_anchor_cards = {}  # {uid: {ANCHOR_ID, visual, personality, behavior, catchphrase, signature_item}}
user_story_contexts = {} # {uid: {"summary": "...", "paras": [...]}}

# ---------- OpenAI ----------
def _chat(messages, temperature=0.7):
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=temperature
        )
        return resp.choices[0].message["content"].strip()
    except Exception as e:
        print("❌ OpenAI error:", e)
        return None

# ---------- 儲存工具 ----------
def save_chat(user_id, role, text):
    try:
        db.collection("users").document(user_id).collection("chat").add({
            "role": role, "text": text, "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print("⚠️ Firebase save_chat failed:", e)

def save_story_summary(user_id, paragraphs):
    try:
        db.collection("users").document(user_id).collection("story")\
          .document("latest_summary").set({
            "paragraphs": paragraphs,
            "updated_at": firestore.SERVER_TIMESTAMP
          })
        print("✅ 已儲存最新故事總結")
    except Exception as e:
        print("⚠️ save_story_summary 失敗：", e)

def save_character_features(user_id, character_features):
    """儲存角色特徵到 Firebase"""
    try:
        db.collection("users").document(user_id).collection("story")\
          .document("character_features").set({
            "features": character_features,
            "base_model": character_features.get("base_model", {}),
            "description": character_features.get("description", ""),
            "updated_at": firestore.SERVER_TIMESTAMP
          })
        print("✅ 已儲存角色特徵")
    except Exception as e:
        print("⚠️ save_character_features 失敗：", e)

def load_character_features(user_id):
    """從 Firebase 載入角色特徵"""
    try:
        doc = db.collection("users").document(user_id).collection("story")\
               .document("character_features").get()
        if doc.exists:
            data = doc.to_dict()
            features = data.get("features", {})
            base_model = data.get("base_model", {})
            description = data.get("description", "")
            
            # 重建完整的角色卡結構
            character_card = {
                **features,
                "base_model": base_model,
                "description": description
            }
            
            print("✅ 已載入角色特徵")
            return character_card
    except Exception as e:
        print("⚠️ load_character_features 失敗：", e)
    return None

def load_latest_story_paragraphs(user_id):
    try:
        doc = db.collection("users").document(user_id).collection("story")\
               .document("latest_summary").get()
        if doc.exists:
            data = doc.to_dict()
            paras = data.get("paragraphs") or []
            if isinstance(paras, list) and paras:
                return paras[:5]
    except Exception as e:
        print("⚠️ load_latest_story_paragraphs 失敗：", e)
    return None

def save_anchor(user_id, anchor):
    try:
        db.collection("users").document(user_id).collection("story")\
          .document("anchor").set(anchor, merge=True)
        print("✅ Anchor Card 已儲存")
    except Exception as e:
        print("⚠️ save_anchor 失敗：", e)

def load_anchor(user_id):
    try:
        doc = db.collection("users").document(user_id).collection("story")\
               .document("anchor").get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        print("⚠️ load_anchor 失敗：", e)
    return None

# ---------- GCS 上傳 ----------
def upload_to_gcs_from_url(url, user_id, prompt):
    tmp_path = None
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            fd, tmp_path = tempfile.mkstemp(prefix="img_", suffix=".png", dir="/tmp")
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024*64):
                    if chunk:
                        f.write(chunk)
        filename = f"{user_id}_{uuid.uuid4().hex}.png"
        blob = gcs_bucket.blob(filename)
        blob.upload_from_filename(tmp_path, content_type="image/png")
        gcs_url = f"https://storage.googleapis.com/{GCS_BUCKET}/{filename}"
        db.collection("users").document(user_id).collection("images").add({
            "url": gcs_url, "prompt": (prompt or "")[:1500], "timestamp": firestore.SERVER_TIMESTAMP
        })
        print("✅ 圖片已上傳至 GCS 並儲存：", gcs_url)
        return gcs_url
    except Exception as e:
        print("❌ GCS upload failed:", e)
        return None
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)
        except Exception:
            pass
        gc.collect()

# ---------- 摘要 ----------
def generate_story_summary(messages):
    prompt = (
        "請將以下對話整理成 5 段完整故事，每段 2–3 句（約 60–120 字），"
        "每段需自然呈現場景、角色、主要動作與關鍵物件，但不要列提綱或加註。"
        "用條列 1.~5.，只輸出故事內容，不要標題。"
    )
    msgs = [{"role":"system","content":prompt}] + messages
    return _chat(msgs, temperature=0.5)

def extract_paragraphs(summary):
    if not summary: return []
    lines = [re.sub(r"^\d+\.\s*","",x.strip()) for x in summary.split("\n") if x.strip()]
    return lines[:5]

# ---------- 規範化（中→英片語） ----------
ZH2EN_COLOR = {
    "灰色":"gray","黑色":"black","白色":"white","紅色":"red","藍色":"blue","綠色":"green",
    "黃色":"yellow","粉色":"pink","紫色":"purple","橙色":"orange","棕色":"brown","咖啡色":"brown"
}
ZH2EN_CLOTHING = {
    "長裙":"long skirt","短裙":"short skirt","連衣裙":"dress","碎花":"floral","花色":"floral",
    "襯衫":"shirt","T恤":"t-shirt","外套":"jacket","大衣":"coat","背帶褲":"suspenders"
}
def normalize_colors(text):
    if not text: return text
    for zh,en in ZH2EN_COLOR.items(): text = re.sub(zh, en, text)
    return text
def normalize_visual_freeform(text):
    text = normalize_colors(text or "")
    # 常見中文服飾詞翻成英文關鍵詞
    for zh,en in ZH2EN_CLOTHING.items():
        text = re.sub(zh, en, text)
    return text

# ---------- Anchor（錨定） ----------
def ensure_anchor(user_id):
    if user_id in user_anchor_cards and user_anchor_cards[user_id]:
        return user_anchor_cards[user_id]
    loaded = load_anchor(user_id)
    if loaded:
        user_anchor_cards[user_id] = loaded
        return loaded
    # 初始空卡（等保底推斷）
    card = {
        "ANCHOR_ID": uuid.uuid4().hex[:6],
        "visual": "", "personality": "", "behavior": "",
        "catchphrase": "", "signature_item": ""
    }
    user_anchor_cards[user_id] = card
    save_anchor(user_id, card)
    return card

def merge_anchor(user_id, patch):
    card = ensure_anchor(user_id)
    for k,v in (patch or {}).items():
        if v: card[k] = v
    if not card.get("ANCHOR_ID"):
        card["ANCHOR_ID"] = uuid.uuid4().hex[:6]
    user_anchor_cards[user_id] = card
    save_anchor(user_id, card)
    return card

def anchor_text(card):
    aid = card.get("ANCHOR_ID","????")
    visual = card.get("visual","human, simple outfit")
    personality = card.get("personality","kind, reliable")
    behavior = card.get("behavior","helps family efficiently")
    sig = card.get("signature_item","")
    base = [
        f"ANCHOR::{aid}",
        f"CHARACTER BIBLE (DO NOT CHANGE): Main character visual: {visual}.",
        "Keep face, hairstyle (length/shape), outfit items, color palette, and body proportions CONSISTENT in all images.",
        "Do NOT change age/gender/ethnicity/hairstyle/outfit/colors unless explicitly instructed."
    ]
    if sig:
        base.append(f"Signature item: {sig}. Show when appropriate.")
    base += [
        f"PERSONALITY: {personality}.",
        f"BEHAVIOR: {behavior}.",
        f"ANCHOR::{aid}"
    ]
    return "\n".join(base)

# —— 保底：從故事內容自動推斷角色藍圖（無需使用者多說）
def infer_anchor_from_story(user_id, paragraphs, raw_context):
    """
    用當前 5 段（或最後對話）推斷主角的視覺/性格/行為/口頭禪/標誌物，全部輸出英文短語。
    """
    sysmsg = (
        "You are a precise story analyst. Based on the given Chinese story paragraphs or notes, "
        "infer the MAIN CHARACTER's blueprint and output JSON with keys: "
        "visual (EN short phrases: hair/eyes/clothes/colors/marks), personality (comma-separated EN traits), "
        "behavior (usual actions/reactions), catchphrase (if any), signature_item (if any). "
        "Be concise; avoid extra text; use lowercase English nouns/adjectives."
    )
    content = "Paragraphs:\n" + "\n".join(paragraphs or []) + "\n\nRaw context:\n" + (raw_context or "")
    res = _chat([{"role":"system","content":sysmsg},{"role":"user","content":content}], temperature=0.2)
    try:
        data = json.loads(res)
    except Exception:
        # 後備一個「職業媽媽」類預設，符合你示例
        data = {
            "visual": "short hair, floral long skirt, office casual, warm smile",
            "personality": "caring, efficient, calm under pressure",
            "behavior": "teleports to help family, balances work and home",
            "catchphrase": "",
            "signature_item": "work laptop"
        }
    # 視覺短語做基本規範化
    data["visual"] = normalize_visual_freeform(data.get("visual",""))
    return data

# ---------- 場景分析（簡版） ----------
def analyze_scene(paragraph, user_id):
    """分析場景，支援任何類型的場景"""
    world_context = user_story_contexts.get(user_id, {})
    
    # 基礎場景分析
    scene = {
        "setting": "general location",
        "time_of_day": "day",
        "mood": "neutral",
        "foreground": "main character in action",
        "background": "environmental elements",
        "main_action": "performing an action",
        "interaction": "interacting with surroundings",
        "key_objects": ""
    }
    
    # 智能場景識別
    if re.search(r"(森林|樹林|公園|花園)", paragraph):
        scene["setting"] = "forest/nature"
    elif re.search(r"(城市|街道|大樓|商店)", paragraph):
        scene["setting"] = "urban/city"
    elif re.search(r"(家裡|房間|廚房|客廳)", paragraph):
        scene["setting"] = "home/indoor"
    elif re.search(r"(學校|教室|操場|圖書館)", paragraph):
        scene["setting"] = "school/educational"
    
    if re.search(r"(夜晚|晚上|深夜|月光)", paragraph):
        scene["time_of_day"] = "night"
    elif re.search(r"(早晨|早上|日出|黃昏)", paragraph):
        scene["time_of_day"] = "morning/sunset"
    
    if re.search(r"(快樂|開心|興奮|歡樂)", paragraph):
        scene["mood"] = "happy/joyful"
    elif re.search(r"(悲傷|難過|憂鬱|緊張)", paragraph):
        scene["mood"] = "sad/melancholy"
    elif re.search(r"(神秘|奇幻|冒險|刺激)", paragraph):
        scene["mood"] = "mysterious/adventurous"
    
    return scene

# ---------- 圖像 Prompt（錨定放最前，無廢話） ----------
def build_image_prompt(user_id, scene, user_extra_desc=""):
    """生成開放的圖像 prompt，支援任何類型的角色和場景"""
    
    # 使用新的角色一致性系統
    character_base = get_character_consistency_prompt(user_id)
    
    # 風格指導 - 確保插畫風格
    style_guide = (
        "Beautiful, detailed illustration in watercolor style. Full scene composition. "
        "Avoid text, letters, words, captions, subtitles, watermark, signature. "
        "Show environment and story action. High quality, artistic illustration."
    )
    
    # 場景描述
    scene_desc = (
        f"Setting: {scene.get('setting', 'general location')}, "
        f"Time: {scene.get('time_of_day', 'day')}, "
        f"Mood: {scene.get('mood', 'neutral')}, "
        f"Action: {scene.get('main_action', 'performing an action')}, "
        f"Background: {scene.get('background', 'environmental elements')}"
    )
    
    # 組合 prompt - 角色描述放在最前面，確保優先級
    parts = [character_base, style_guide, scene_desc]
    if user_extra_desc:
        parts.append(f"User requirements: {user_extra_desc}")
    
    prompt = " ".join(parts)
    
    # 負面 prompt - 加強角色一致性要求
    negative = (
        "text, letters, words, captions, subtitles, watermark, signature, "
        "low quality, blurry, different character, change hairstyle, change outfit, "
        "age change, gender change, inconsistent appearance, wrong character"
    )
    
    return prompt, negative

# ---------- Leonardo API ----------
def leonardo_headers():
    return {
        "Authorization": f"Bearer {LEONARDO_API_KEY.strip()}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def leonardo_tti(payload):
    url = f"{LEO_BASE}/generations"
    r = requests.post(url, headers=leonardo_headers(), json=payload, timeout=60)
    if not r.ok:
        print("❌ Leonardo POST 失敗:", r.status_code, r.text)
    r.raise_for_status()
    data = r.json()
    return data["sdGenerationJob"]["generationId"]

def leonardo_poll(gen_id, timeout=180):
    url = f"{LEO_BASE}/generations/{gen_id}"
    start = time.time()
    while time.time()-start < timeout:
        time.sleep(4)
        try:
            r = requests.get(url, headers=leonardo_headers(), timeout=30)
            if not r.ok:
                print(f"❌ Leonardo GET 失敗: {r.status_code}, {r.text}")
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("generations_by_pk"):
                generation_data = data["generations_by_pk"]
                status = generation_data.get("status")
                if status == "COMPLETE":
                    images = generation_data.get("generated_images", [])
                    if images:
                        gi = images[0]
                        return gi.get("url"), gi.get("id")
                    return None, None
                elif status == "FAILED":
                    return None, None
            else:
                print(f"⚠️ 回應格式異常: {data}")
        except Exception as e:
            print("❌ poll error:", e)
            traceback.print_exc()
    print(f"⏰ 輪詢超時 ({timeout}s)，生成 ID: {gen_id}")
    return None, None

def generate_leonardo_image(*, user_id, prompt, negative_prompt, seed, init_image_id=None, init_strength=None):
    payload = {
        "modelId": LEO_MODEL,
        "prompt": prompt[:1500],
        "num_images": 1,
        "width": IMG_W, "height": IMG_H,
        "ultra": False,
        "enhancePrompt": False,
        "negative_prompt": negative_prompt,
        "seed": int(seed)
    }
    if init_image_id and init_strength is not None:
        payload["init_image_id"] = init_image_id
        payload["init_strength"] = float(init_strength)

    print("🎨 Leonardo payload =>", json.dumps(payload, ensure_ascii=False)[:900])
    try:
        gen_id = leonardo_tti(payload)
        url, image_id = leonardo_poll(gen_id)
        if url:
            gcs_url = upload_to_gcs_from_url(url, user_id, prompt)
            return {"url": gcs_url, "image_id": image_id}
    except requests.HTTPError as e:
        if init_image_id and "Unexpected variable" in str(e):
            print("↩️ 降級 t2i 重試")
            return generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=negative_prompt,
                seed=seed, init_image_id=None, init_strength=None
            )
        print("❌ Leonardo HTTP 錯誤：", e)
    except Exception as e:
        print("❌ Leonardo 其他錯誤：", e)
        traceback.print_exc()
    return None

# ---------- 引導與格式 ----------
base_system_prompt = (
    "你是「小繪」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助用戶創作任何類型的故事。\n"
    "請用簡潔、好讀的語氣回應；每則訊息盡量不超過 35 字並適當分段。\n"
    "第一階段：以『回述 + 肯定 + 輕量補問 1–2 題』來引導補齊人事時地物與動作/情緒。\n"
    "只有在使用者說「整理/總結」或要求繪圖且無段落摘要時，才產生摘要（五段乾淨段落）。\n"
    "請自稱「小繪」。"
)

def format_reply(text):
    return re.sub(r'([。！？])\s*', r'\1\n', text)

def natural_guidance(last_user_text):
    """智能引導用戶構建故事"""
    brief = last_user_text if len(last_user_text) <= 40 else last_user_text[:40] + "…"
    asks = []
    
    # 檢查故事的基本要素
    if not re.search(r"(叫|名|主角|角色|他|她|它)", last_user_text):
        asks.append("主角或角色是什麼呢？")
    
    if not re.search(r"(在哪|哪裡|什麼地方|場景|森林|城市|家裡|學校)", last_user_text):
        asks.append("這段發生在哪裡呢？")
    
    if not re.search(r"(做什麼|發生|遇到|準備|解決|發現|幫助|尋找)", last_user_text):
        asks.append("這段想發生什麼事情呢？")
    
    if not re.search(r"(什麼時候|時間|早上|晚上|白天|夜晚)", last_user_text):
        asks.append("這段發生在什麼時候呢？")
    
    if not asks: 
        asks = ["想再加哪個小細節？"]
    
    return f"我聽到了：{brief}\n很有畫面感！\n{asks[0]}"

def check_story_completeness(paragraphs):
    """檢查故事完整性，確保每個段落都有足夠細節"""
    if not paragraphs:
        return False, "故事還沒有開始構建"
    
    incomplete_paragraphs = []
    for i, para in enumerate(paragraphs):
        # 檢查段落是否包含基本要素
        has_character = re.search(r"(主角|角色|他|她|它|名字)", para)
        has_setting = re.search(r"(在|位於|來到|進入|森林|城市|家裡|學校)", para)
        has_action = re.search(r"(做|發生|遇到|準備|解決|發現|幫助|尋找|走|跑|看|說)", para)
        
        if not (has_character and has_setting and has_action):
            incomplete_paragraphs.append(i + 1)
    
    if incomplete_paragraphs:
        return False, f"第 {', '.join(map(str, incomplete_paragraphs))} 段需要更多細節"
    
    return True, "故事完整"

def suggest_story_improvements(paragraphs):
    """建議如何改善故事段落"""
    suggestions = []
    
    for i, para in enumerate(paragraphs):
        para_suggestions = []
        
        # 檢查角色描述
        if not re.search(r"(穿|戴|頭髮|眼睛|身高|年齡)", para):
            para_suggestions.append("描述角色的外觀特徵")
        
        # 檢查場景描述
        if not re.search(r"(顏色|形狀|大小|光線|天氣)", para):
            para_suggestions.append("描述場景的視覺細節")
        
        # 檢查動作描述
        if not re.search(r"(如何|怎樣|表情|情緒|感覺)", para):
            para_suggestions.append("描述角色的動作和情緒")
        
        if para_suggestions:
            suggestions.append(f"第 {i+1} 段：{', '.join(para_suggestions)}")
    
    return suggestions

def build_detailed_scene(paragraph, user_id):
    """根據段落構建詳細的場景描述"""
    # 基礎場景分析
    scene = analyze_scene(paragraph, user_id)
    
    # 智能補充場景細節
    if "森林" in paragraph or "樹林" in paragraph:
        scene["background"] = "dense forest with tall trees, green foliage, natural sunlight filtering through"
        scene["mood"] = "peaceful and natural"
    elif "城市" in paragraph or "街道" in paragraph:
        scene["background"] = "urban cityscape with buildings, streets, city atmosphere"
        scene["mood"] = "busy and vibrant"
    elif "家裡" in paragraph or "房間" in paragraph:
        scene["background"] = "cozy indoor setting with furniture, warm lighting, home atmosphere"
        scene["mood"] = "comfortable and familiar"
    
    # 根據動作補充前景
    if "走" in paragraph or "跑" in paragraph:
        scene["foreground"] = "main character in motion, showing movement and energy"
    elif "看" in paragraph or "觀察" in paragraph:
        scene["foreground"] = "main character looking around, showing curiosity and attention"
    elif "說" in paragraph or "對話" in paragraph:
        scene["foreground"] = "main character speaking or communicating, showing expression and emotion"
    
    return scene

# ---------- Flask ----------
@app.route("/")
def root():
    return "LINE GPT Webhook is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    if not signature:
        print("⚠️ Missing X-Line-Signature — non-LINE request. Ignored.")
        return "OK"
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ---------- 狀態工具 ----------
def reset_session(user_id):
    user_sessions[user_id] = {"messages": [], "story_mode": True, "summary": "", "paras": []}
    user_seeds[user_id] = random.randint(100000, 999999)
    user_last_images[user_id] = {}
    user_story_contexts[user_id] = {"summary": "", "paras": []}
    
    # 嘗試載入已儲存的角色特徵
    saved_character = load_character_features(user_id)
    if saved_character:
        user_character_cards[user_id] = saved_character
        print(f"🔄 已載入已儲存的角色特徵: {saved_character.get('description', '')[:100]}...")
    else:
        # 重置角色特徵
        user_character_cards[user_id] = {}
        print(f"🔄 已重置角色特徵")
    
    print(f"✅ Reset session for {user_id}, seed={user_seeds[user_id]}")

# ---------- 背景任務（無廢話） ----------
GEN_SEMAPHORE = threading.Semaphore(2)

def _ensure_paragraphs(user_id, sess):
    paras = load_latest_story_paragraphs(user_id) or sess.get("paras") or []
    if not paras:
        # 從最近對話蒐集故事性訊息
        noise = re.compile(r"(幫我畫第|請畫第|畫第|整理|總結|定妝|角色設定|更新角色)")
        story_texts = [m["content"] for m in sess["messages"] if m.get("role")=="user" and not noise.search(m.get("content",""))]
        if story_texts:
            compact = [{"role":"user","content":"\n".join(story_texts[-8:])}]
            summary = generate_story_summary(compact)
            sess["summary"] = summary
            paras = extract_paragraphs(summary)
            sess["paras"] = paras
            if paras: save_story_summary(user_id, paras)
    return paras

# ---------- 主處理 ----------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    reply_token = event.reply_token
    print(f"📩 {user_id}：{text}")

    try:
        # 啟動
        if re.search(r"(開始說故事|說故事|講個故事|一起來講故事吧|我們來講故事吧)", text):
            reset_session(user_id)
            # 啟動後不多話
            line_bot_api.reply_message(reply_token, TextSendMessage("好的，直接描述故事或叫我畫第幾段即可。"))
            return

        # 對話緩存
        sess = user_sessions.setdefault(user_id, {"messages": [], "summary": "", "paras": []})
        sess["messages"].append({"role":"user","content":text})
        if len(sess["messages"]) > 60: sess["messages"] = sess["messages"][-60:]
        save_chat(user_id, "user", text)

        # 智能角色特徵提取和更新
        if update_character_card(user_id, text):
            print(f"✨ 角色卡已更新: {user_character_cards[user_id]['description'][:100]}...")
            # 更新基底人物模組
            update_character_model(user_id, extract_character_features(text))

        # 整理 / 總結
        if re.search(r"(整理|總結|summary)", text):
            msgs = [{"role":"system","content":base_system_prompt}] + sess["messages"][-40:]
            summary = generate_story_summary(msgs)
            sess["summary"] = summary
            paras = extract_paragraphs(summary)
            sess["paras"] = paras
            if paras: save_story_summary(user_id, paras)
            
            # 使用創意聯想增強故事
            enhanced_elements = enhance_story_with_associations(summary)
            if enhanced_elements:
                enhanced_text = f"✨ 故事總結完成！\n\n📖 主要情節：\n{summary}\n\n💡 創意聯想：\n" + "\n".join([f"• {elem}" for elem in enhanced_elements[:5]])
            else:
                enhanced_text = f"✨ 故事總結完成！\n\n📖 主要情節：\n{summary}"
            
            line_bot_api.reply_message(reply_token, TextSendMessage(enhanced_text))
            save_chat(user_id, "assistant", enhanced_text)
            return

        # 定妝
        if "定妝" in text:
            line_bot_api.reply_message(reply_token, TextSendMessage("收到，我先做定妝照，畫好就傳給你～"))
            threading.Thread(target=bg_generate_and_push_portrait, args=(user_id,), daemon=True).start()
            return

        # 查看角色卡
        if "角色卡" in text or "查看角色" in text:
            if user_id in user_character_cards and user_character_cards[user_id]:
                character_info = user_character_cards[user_id]
                response = "📋 當前角色卡：\n"
                for key, value in character_info.items():
                    if key not in ["description", "base_model"]:
                        response += f"• {key}: {value}\n"
                if "description" in character_info:
                    response += f"\n🎨 完整描述：\n{character_info['description']}"
                if "base_model" in character_info:
                    base_model = character_info["base_model"]
                    response += f"\n\n🔧 基底模組：\n• 視覺特徵: {base_model['visual_base']['hair_style']}, {base_model['visual_base']['eye_color']}, {base_model['visual_base']['body_type']}"
                    response += f"\n• 服裝特徵: {base_model['clothing_base']['main_outfit']}, {base_model['clothing_base']['color_scheme']}"
            else:
                response = "還沒有建立角色卡，請先描述一下角色特徵吧！"
            line_bot_api.reply_message(reply_token, TextSendMessage(response))
            return

        # 畫第 N 段
        draw_pat = r"(幫我畫第[一二三四五12345]段故事的圖|請畫第[一二三四五12345]段故事的插圖|畫第[一二三四五12345]段故事的圖)"
        if re.search(draw_pat, text):
            m = re.search(r"[一二三四五12345]", text)
            idx_map = {'一':1,'二':2,'三':3,'四':4,'五':5,'1':1,'2':2,'3':3,'4':4,'5':5}
            n = idx_map.get(m.group(0),1) - 1
            extra = re.sub(draw_pat, "", text).strip(" ，,。.!！")

            # 先確保有故事段落
            paras = load_latest_story_paragraphs(user_id)
            if not paras:
                story_user_texts = [m["content"] for m in sess["messages"]
                                    if m.get("role")=="user" and not re.search(r"(幫我畫|請畫|畫|整理|總結|定妝)", m.get("content",""))]
                if story_user_texts:
                    compact_msgs = [{"role":"user","content":"\n".join(story_user_texts[-8:])}]
                    summary = generate_story_summary(compact_msgs)
                    sess["summary"] = summary
                    paras = extract_paragraphs(summary)
                    sess["paras"] = paras
                    if paras: save_story_summary(user_id, paras)

            if not paras:
                line_bot_api.reply_message(reply_token, TextSendMessage("我需要再多一點故事內容，才能開始畫第 1 段喔～"))
                return

            # 檢查故事完整性
            is_complete, message = check_story_completeness(paras)
            if not is_complete:
                # 提供具體的改善建議
                suggestions = suggest_story_improvements(paras)
                if suggestions:
                    response = f"{message}\n\n💡 建議改善：\n" + "\n".join(suggestions[:3])
                else:
                    response = f"{message}\n\n請再補充一些細節，讓故事更生動！"
                line_bot_api.reply_message(reply_token, TextSendMessage(response))
                return

            # 如果有額外描述，更新角色卡和基底模組
            if extra:
                if update_character_card(user_id, extra):
                    update_character_model(user_id, extract_character_features(extra))
                    line_bot_api.reply_message(reply_token, TextSendMessage(f"收到！已更新角色特徵：{extra}"))
                else:
                    line_bot_api.reply_message(reply_token, TextSendMessage(f"收到！開始畫第 {n+1} 段"))

            line_bot_api.reply_message(reply_token, TextSendMessage(f"收到，我開始畫第 {n+1} 段，完成就傳給你～"))
            threading.Thread(target=bg_generate_and_push_draw, args=(user_id,n,extra), daemon=True).start()
            return

        # 一般引導 - 使用故事結構模型
        current_stage, stage_index = analyze_story_stage(sess["messages"])
        guidance = get_story_guidance(current_stage, text)
        
        # 如果有創意聯想，加入引導中
        enhanced_elements = enhance_story_with_associations(text)
        if enhanced_elements:
            guidance += f"\n\n💡 創意聯想：\n" + "\n".join([f"• {elem}" for elem in enhanced_elements[:3]])
        
        line_bot_api.reply_message(reply_token, TextSendMessage(guidance))
        save_chat(user_id, "assistant", guidance)

    except Exception as e:
        print("❌ 發生錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage("小繪出了一點小狀況，稍後再試 🙇"))

# ---------- 故事結構模型 ----------
STORY_STRUCTURE = {
    "開端": {
        "description": "介紹主角、故事背景、以及主角當前的困境或目標",
        "elements": ["主角介紹", "背景設定", "初始狀態", "動機目標"],
        "prompts": ["主角是誰？", "故事發生在哪裡？", "主角想要什麼？", "遇到了什麼問題？"]
    },
    "衝突": {
        "description": "主角遇到挑戰，故事的張力開始增加",
        "elements": ["挑戰描述", "困難程度", "對手的特徵", "內心的掙扎"],
        "prompts": ["主角遇到了什麼挑戰？", "這個挑戰有多困難？", "有對手嗎？", "主角內心怎麼想？"]
    },
    "高潮": {
        "description": "故事的轉捩點，主角必須做出關鍵的選擇或行動",
        "elements": ["關鍵選擇", "行動描述", "轉捩點", "解決方案"],
        "prompts": ["主角做了什麼決定？", "如何行動？", "關鍵時刻是什麼？", "怎麼解決問題？"]
    },
    "結尾": {
        "description": "故事的結局，主角的命運被決定",
        "elements": ["結果描述", "情感變化", "學習成長", "未來展望"],
        "prompts": ["最後結果如何？", "主角有什麼感受？", "學到了什麼？", "未來會怎樣？"]
    }
}

def analyze_story_stage(user_messages):
    """分析當前故事發展到哪個階段"""
    if not user_messages:
        return "開端", 0
    
    # 簡單的階段判斷邏輯
    story_text = " ".join([msg.get("content", "") for msg in user_messages if msg.get("role") == "user"])
    
    if re.search(r"(遇到|挑戰|困難|問題|敵人|掙扎)", story_text):
        if re.search(r"(解決|克服|成功|勝利|結局|結束)", story_text):
            return "結尾", 3
        else:
            return "衝突", 1
    elif re.search(r"(決定|選擇|行動|關鍵|轉捩)", story_text):
        return "高潮", 2
    else:
        return "開端", 0

def get_story_guidance(stage, user_text):
    """根據故事階段提供智能引導"""
    stage_info = STORY_STRUCTURE[stage]
    
    # 檢查當前階段缺少什麼元素
    missing_elements = []
    for element in stage_info["elements"]:
        if not has_element_in_text(element, user_text):
            missing_elements.append(element)
    
    if missing_elements:
        # 提供具體的引導問題
        guidance_questions = []
        for element in missing_elements[:2]:  # 最多問2個問題
            if element in stage_info["prompts"]:
                guidance_questions.append(stage_info["prompts"][stage_info["elements"].index(element)])
        
        if guidance_questions:
            return f"🎭 現在是故事的「{stage}」階段！\n\n💡 建議補充：\n" + "\n".join([f"• {q}" for q in guidance_questions])
    
    # 如果當前階段完整，引導進入下一階段
    next_stage = get_next_stage(stage)
    if next_stage:
        next_info = STORY_STRUCTURE[next_stage]
        return f"✨ 「{stage}」階段完成！\n\n🎬 接下來進入「{next_stage}」階段：\n{next_info['description']}\n\n💭 你想描述什麼呢？"
    
    return f"🎉 故事已經很完整了！\n\n📝 你可以說「整理」來總結故事，或「幫我畫第X段故事的圖」來生成插圖！"

def has_element_in_text(element, text):
    """檢查文本是否包含特定元素"""
    element_patterns = {
        "主角介紹": r"(主角|角色|他|她|它|名字|叫)",
        "背景設定": r"(在|位於|來到|進入|森林|城市|家裡|學校|太空|星球)",
        "初始狀態": r"(原本|開始|以前|一直|總是)",
        "動機目標": r"(想要|希望|夢想|目標|尋找|得到|解決)",
        "挑戰描述": r"(遇到|挑戰|困難|問題|危險|阻礙)",
        "困難程度": r"(很困難|非常|極度|艱難|簡單|容易)",
        "對手的特徵": r"(敵人|對手|壞人|怪物|野獸|競爭者)",
        "內心的掙扎": r"(猶豫|掙扎|害怕|擔心|困惑|矛盾)",
        "關鍵選擇": r"(決定|選擇|必須|關鍵|重要|轉捩)",
        "行動描述": r"(做|行動|開始|執行|完成|實現)",
        "轉捩點": r"(突然|忽然|關鍵時刻|轉捩點|重要時刻)",
        "解決方案": r"(解決|克服|戰勝|成功|勝利|完成)",
        "結果描述": r"(最後|結果|終於|成功|失敗|完成)",
        "情感變化": r"(開心|快樂|悲傷|難過|興奮|感動)",
        "學習成長": r"(學到|成長|改變|進步|理解|明白)",
        "未來展望": r"(未來|以後|接下來|明天|將來|希望)"
    }
    
    if element in element_patterns:
        return bool(re.search(element_patterns[element], text))
    return False

def get_next_stage(current_stage):
    """獲取下一個故事階段"""
    stage_order = ["開端", "衝突", "高潮", "結尾"]
    try:
        current_index = stage_order.index(current_stage)
        if current_index < len(stage_order) - 1:
            return stage_order[current_index + 1]
    except ValueError:
        pass
    return None

# ---------- 創意聯想與情節補完 ----------
def creative_association(keyword):
    """根據關鍵詞進行創意聯想"""
    associations = {
        "太空人": ["宇宙飛船", "遙遠的星球", "外星生物", "孤獨感", "無重力", "星空", "探索"],
        "森林": ["樹木", "鳥叫聲", "陽光透過樹葉", "小徑", "野生動物", "清新的空氣", "神秘感"],
        "魔法": ["魔杖", "咒語", "魔法書", "神秘力量", "不可思議", "奇幻世界", "魔法師"],
        "寶藏": ["地圖", "冒險", "危險", "財富", "歷史", "秘密", "探索"],
        "友誼": ["信任", "支持", "陪伴", "理解", "分享", "快樂", "溫暖"],
        "勇氣": ["面對困難", "克服恐懼", "堅持", "勇敢", "挑戰", "成長", "力量"]
    }
    
    for key, values in associations.items():
        if key in keyword or any(val in keyword for val in values):
            return values
    return []

def enhance_story_with_associations(story_text):
    """使用創意聯想增強故事"""
    enhanced_elements = []
    
    # 尋找關鍵詞並聯想
    for keyword in ["太空人", "森林", "魔法", "寶藏", "友誼", "勇氣"]:
        if keyword in story_text:
            associations = creative_association(keyword)
            enhanced_elements.extend(associations[:3])  # 取前3個聯想
    
    return enhanced_elements

# ---------- 基底人物模組系統 ----------
def create_base_character_model(user_id, character_features):
    """創建基底人物模組 - 更健壯的版本"""
    try:
        base_model = {
            "id": f"char_{user_id}_{uuid.uuid4().hex[:8]}",
            "features": character_features,
            "visual_base": {
                "face_shape": character_features.get("臉型", "standard"),
                "hair_style": character_features.get("髮型", "standard"),
                "eye_color": character_features.get("眼色", character_features.get("眼型", "standard")),
                "body_type": character_features.get("體型", "standard"),
                "age_group": character_features.get("年齡", "young")
            },
            "clothing_base": {
                "main_outfit": character_features.get("裙子", character_features.get("上衣", character_features.get("褲子", "standard"))),
                "color_scheme": character_features.get("主要顏色", "neutral"),
                "accessories": character_features.get("配件", [])
            },
            "special_features": {
                "species": character_features.get("物種", "human"),
                "abilities": character_features.get("能力", []),
                "personality": character_features.get("性格", "neutral"),
                "equipment": character_features.get("裝備", []),
                "environment": character_features.get("環境", "normal")
            },
            "personality_traits": [],
            "created_at": datetime.now().isoformat()
        }
        
        # 處理列表類型的特徵
        for key in ["配件", "能力", "裝備"]:
            if key in character_features and isinstance(character_features[key], str):
                base_model["special_features"][key.replace("配件", "accessories").replace("能力", "abilities").replace("裝備", "equipment")] = [character_features[key]]
        
        return base_model
        
    except Exception as e:
        print(f"⚠️ 創建角色模組時發生錯誤: {e}")
        # 返回基本模組
        return {
            "id": f"char_{user_id}_{uuid.uuid4().hex[:8]}",
            "features": character_features,
            "visual_base": {"face_shape": "standard", "hair_style": "standard", "eye_color": "standard", "body_type": "standard", "age_group": "young"},
            "clothing_base": {"main_outfit": "standard", "color_scheme": "neutral", "accessories": []},
            "special_features": {"species": "human", "abilities": [], "personality": "neutral", "equipment": [], "environment": "normal"},
            "personality_traits": [],
            "created_at": datetime.now().isoformat()
        }

def update_character_model(user_id, new_features):
    """更新基底人物模組 - 更健壯的版本"""
    try:
        if user_id not in user_character_cards:
            user_character_cards[user_id] = {}
        
        # 更新特徵
        user_character_cards[user_id].update(new_features)
        
        # 創建或更新基底模組
        if "base_model" not in user_character_cards[user_id]:
            user_character_cards[user_id]["base_model"] = create_base_character_model(user_id, new_features)
        else:
            # 更新現有模組
            base_model = user_character_cards[user_id]["base_model"]
            base_model["features"].update(new_features)
            
            # 更新視覺特徵
            for key, value in new_features.items():
                if key in ["髮型", "眼型", "體型", "年齡", "臉型"]:
                    if "visual_base" not in base_model:
                        base_model["visual_base"] = {}
                    base_model["visual_base"][key] = value
                elif key in ["裙子", "上衣", "褲子", "主要顏色"]:
                    if "clothing_base" not in base_model:
                        base_model["clothing_base"] = {}
                    if key in ["裙子", "上衣", "褲子"]:
                        base_model["clothing_base"]["main_outfit"] = value
                    elif key == "主要顏色":
                        base_model["clothing_base"]["color_scheme"] = value
                elif key in ["物種", "能力", "性格", "裝備", "環境"]:
                    if "special_features" not in base_model:
                        base_model["special_features"] = {}
                    base_model["special_features"][key] = value
        
        # 生成角色描述
        character_desc = build_character_description(user_character_cards[user_id])
        user_character_cards[user_id]["description"] = character_desc
        
        # 🔑 關鍵：儲存到 Firebase
        save_character_features(user_id, user_character_cards[user_id])
        print(f"💾 基底模組已更新並儲存到 Firebase")
        
        return user_character_cards[user_id]["base_model"]
        
    except Exception as e:
        print(f"⚠️ 更新角色模組時發生錯誤: {e}")
        # 如果更新失敗，至少保存基本特徵
        if user_id not in user_character_cards:
            user_character_cards[user_id] = {}
        user_character_cards[user_id].update(new_features)
        return None

def get_character_consistency_prompt(user_id):
    """獲取角色一致性 prompt - 更健壯的版本"""
    try:
        if user_id not in user_character_cards or "base_model" not in user_character_cards[user_id]:
            return "Main character with unique features. Maintain consistent appearance across all images."
        
        base_model = user_character_cards[user_id]["base_model"]
        features = base_model.get("features", {})
        
        # 構建一致性 prompt
        consistency_parts = [
            "Main character with consistent appearance:",
            f"Face: {base_model.get('visual_base', {}).get('face_shape', 'standard')} shape",
            f"Hair: {base_model.get('visual_base', {}).get('hair_style', 'standard')}",
            f"Eyes: {base_model.get('visual_base', {}).get('eye_color', 'standard')}",
            f"Body: {base_model.get('visual_base', {}).get('body_type', 'standard')} build",
            f"Age: {base_model.get('visual_base', {}).get('age_group', 'young')}"
        ]
        
        # 物種特徵
        species = base_model.get("special_features", {}).get("species", "human")
        if species and species != "human":
            consistency_parts.append(f"Species: {species}")
        
        # 服裝特徵
        if "裙子" in features:
            consistency_parts.append(f"Clothing: {features['裙子']} in {features.get('主要顏色', 'neutral')} color")
        elif "上衣" in features:
            consistency_parts.append(f"Clothing: {features['上衣']} in {features.get('主要顏色', 'neutral')} color")
        elif "褲子" in features:
            consistency_parts.append(f"Clothing: {features['褲子']} in {features.get('主要顏色', 'neutral')} color")
        
        # 特殊能力
        abilities = base_model.get("special_features", {}).get("能力", [])
        if abilities:
            if isinstance(abilities, list):
                consistency_parts.append(f"Powers: {', '.join(abilities[:3])}")
            else:
                consistency_parts.append(f"Powers: {abilities}")
        
        consistency_parts.append("Maintain exact same appearance, facial features, hairstyle, and proportions across all images.")
        
        return " ".join(consistency_parts)
        
    except Exception as e:
        print(f"⚠️ 生成角色一致性 prompt 時發生錯誤: {e}")
        return "Main character with unique features. Maintain consistent appearance across all images."

def extract_character_features(text):
    """提取角色特徵，支援任何類型的角色描述"""
    features = {}
    
    # 服裝特徵 - 更靈活的匹配
    clothing_patterns = {
        "裙子": r"(長裙|短裙|連衣裙|百褶裙|蓬蓬裙|禮服|洋裝|裙|dress|skirt)",
        "上衣": r"(T恤|襯衫|毛衣|外套|夾克|背心|衛衣|針織衫|上衣|shirt|jacket|sweater)",
        "褲子": r"(牛仔褲|休閒褲|短褲|長褲|運動褲|西裝褲|褲|pants|jeans)",
        "鞋子": r"(運動鞋|皮鞋|靴子|涼鞋|高跟鞋|平底鞋|鞋|shoes|boots)",
        "配件": r"(帽子|眼鏡|項鍊|手錶|包包|圍巾|手套|配件|accessories)"
    }
    
    for key, pattern in clothing_patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            features[key] = match.group(1)
    
    # 顏色特徵 - 支援更多顏色
    color_patterns = {
        "主要顏色": r"(灰色|黑色|白色|紅色|藍色|綠色|黃色|粉色|紫色|棕色|橙色|金色|銀色|彩虹色|透明|漸層|混色)",
        "髮色": r"(黑髮|金髮|棕髮|紅髮|銀髮|白髮|灰髮|藍髮|綠髮|紫髮|彩虹髮|漸層髮)",
        "眼色": r"(黑眼|藍眼|綠眼|棕眼|灰眼|紫眼|金眼|紅眼|異色瞳|彩虹眼)"
    }
    
    for key, pattern in color_patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            features[key] = match.group(1)
    
    # 身體特徵 - 更廣泛的匹配
    body_patterns = {
        "髮型": r"(長髮|短髮|捲髮|直髮|馬尾|辮子|盤髮|瀏海|中分|旁分|爆炸頭|光頭|禿頭|假髮|染髮)",
        "眼型": r"(大眼睛|小眼睛|圓眼|細長眼|單眼皮|雙眼皮|三眼皮|異色瞳|發光眼|機械眼)",
        "體型": r"(高挑|嬌小|苗條|豐滿|健壯|纖細|圓潤|肌肉|肥胖|瘦弱|正常|特殊)",
        "年齡": r"(小孩|嬰兒|幼兒|兒童|青少年|年輕人|成年人|中年人|老年人|老人|長壽|永生)"
    }
    
    for key, pattern in body_patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            features[key] = match.group(1)
    
    # 特殊特徵 - 支援更多物種和能力
    special_patterns = {
        "物種": r"(人類|精靈|獸人|機器人|天使|惡魔|妖精|龍|貓|狗|外星人|吸血鬼|狼人|殭屍|幽靈|神|半神|混血|變種人|賽博格)",
        "能力": r"(魔法|飛行|隱身|變身|治癒|預言|讀心|瞬移|控制元素|時間控制|空間控制|重力控制|心靈控制|超能力|科技能力|武術|格鬥|射擊|駕駛|烹飪|藝術|音樂|寫作)",
        "職業": r"(學生|老師|醫生|警察|魔法師|戰士|商人|農夫|藝術家|科學家|工程師|律師|會計師|廚師|司機|飛行員|太空人|探險家|考古學家|記者|作家|演員|歌手|舞者|運動員)"
    }
    
    for key, pattern in special_patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            features[key] = match.group(1)
    
    # 個性特徵 - 更豐富的描述
    personality_patterns = {
        "性格": r"(勇敢|溫柔|活潑|安靜|聰明|善良|堅強|害羞|開朗|認真|冷酷|熱情|神秘|瘋狂|理性|感性|樂觀|悲觀|幽默|嚴肅|懶惰|勤奮|固執|靈活|忠誠|背叛|獨立|依賴)",
        "情緒": r"(開心|快樂|悲傷|難過|憤怒|生氣|害怕|恐懼|驚訝|震驚|困惑|迷茫|興奮|激動|平靜|冷靜|緊張|焦慮|放鬆|舒適|滿足|不滿|期待|失望|希望|絕望)"
    }
    
    for key, pattern in personality_patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            features[key] = match.group(1)
    
    # 新增：支援自定義特徵
    custom_patterns = {
        "特殊標記": r"(胎記|疤痕|紋身|刺青|痣|雀斑|斑點|傷痕|燒傷|手術痕跡)",
        "裝備": r"(武器|盾牌|盔甲|頭盔|護具|背包|腰帶|手套|靴子|斗篷|披風|圍巾|領帶|領結)",
        "環境": r"(太空|海底|火山|雪山|沙漠|熱帶|寒帶|溫帶|極地|地下|高空|深海|外太空|異世界|平行宇宙|未來世界|古代世界|現代世界)"
    }
    
    for key, pattern in custom_patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            features[key] = match.group(1)
    
    # 如果沒有匹配到任何預定義特徵，嘗試提取一般描述
    if not features:
        # 提取任何看起來像特徵的描述
        general_features = re.findall(r"([一-龯]{2,6}(?:的|是|有|穿|戴|拿|帶|使用|擁有))", text)
        if general_features:
            features["自定義特徵"] = general_features[:3]  # 最多取3個
    
    return features

def update_character_card(user_id, text):
    """更新角色卡，返回是否成功更新"""
    try:
        features = extract_character_features(text)
        
        if not features:
            return False
        
        # 更新角色卡
        if user_id not in user_character_cards:
            user_character_cards[user_id] = {}
        
        user_character_cards[user_id].update(features)
        
        # 生成角色描述
        character_desc = build_character_description(user_character_cards[user_id])
        user_character_cards[user_id]["description"] = character_desc
        
        # 儲存到 Firebase
        save_character_features(user_id, user_character_cards[user_id])
        
        print(f"✨ 角色卡已更新並儲存: {character_desc[:100]}...")
        return True
        
    except Exception as e:
        print(f"⚠️ 更新角色卡時發生錯誤: {e}")
        return False

def build_character_description(character_info):
    """根據角色特徵構建詳細的英文描述 - 更健壯的版本"""
    try:
        if not character_info:
            return "Main character with unique features"
        
        description_parts = ["Main character:"]
        
        # 基本外觀
        if "物種" in character_info:
            description_parts.append(f"Species: {character_info['物種']}")
        
        if "年齡" in character_info:
            description_parts.append(f"Age: {character_info['年齡']}")
        
        # 身體特徵
        if "髮型" in character_info:
            description_parts.append(f"Hair: {character_info['髮型']}")
        
        if "髮色" in character_info:
            description_parts.append(f"Hair color: {character_info['髮色']}")
        
        if "眼型" in character_info:
            description_parts.append(f"Eyes: {character_info['眼型']}")
        
        if "眼色" in character_info:
            description_parts.append(f"Eye color: {character_info['眼色']}")
        
        if "體型" in character_info:
            description_parts.append(f"Body: {character_info['體型']}")
        
        # 服裝特徵
        if "裙子" in character_info:
            description_parts.append(f"Clothing: {character_info['裙子']}")
        elif "上衣" in character_info:
            description_parts.append(f"Clothing: {character_info['上衣']}")
        elif "褲子" in character_info:
            description_parts.append(f"Clothing: {character_info['褲子']}")
        
        if "主要顏色" in character_info:
            description_parts.append(f"Main color: {character_info['主要顏色']}")
        
        if "鞋子" in character_info:
            description_parts.append(f"Shoes: {character_info['鞋子']}")
        
        if "配件" in character_info:
            accessories = character_info['配件']
            if isinstance(accessories, list):
                description_parts.append(f"Accessories: {', '.join(accessories)}")
            else:
                description_parts.append(f"Accessories: {accessories}")
        
        # 特殊能力
        if "能力" in character_info:
            abilities = character_info['能力']
            if isinstance(abilities, list):
                description_parts.append(f"Powers: {', '.join(abilities)}")
            else:
                description_parts.append(f"Powers: {abilities}")
        
        # 個性特徵
        if "性格" in character_info:
            description_parts.append(f"Personality: {character_info['性格']}")
        
        if "情緒" in character_info:
            description_parts.append(f"Mood: {character_info['情緒']}")
        
        # 特殊標記
        if "特殊標記" in character_info:
            description_parts.append(f"Special marks: {character_info['特殊標記']}")
        
        # 裝備
        if "裝備" in character_info:
            equipment = character_info['裝備']
            if isinstance(equipment, list):
                description_parts.append(f"Equipment: {', '.join(equipment)}")
            else:
                description_parts.append(f"Equipment: {equipment}")
        
        # 環境
        if "環境" in character_info:
            description_parts.append(f"Environment: {character_info['環境']}")
        
        # 自定義特徵
        if "自定義特徵" in character_info:
            custom = character_info['自定義特徵']
            if isinstance(custom, list):
                description_parts.append(f"Custom features: {', '.join(custom)}")
            else:
                description_parts.append(f"Custom features: {custom}")
        
        # 一致性要求
        description_parts.append("Maintain exact same appearance, facial features, hairstyle, and proportions across all images.")
        
        return " ".join(description_parts)
        
    except Exception as e:
        print(f"⚠️ 構建角色描述時發生錯誤: {e}")
        return "Main character with unique features. Maintain consistent appearance across all images."

def bg_generate_and_push_draw(user_id, n, extra_desc):
    """背景生成第 n 段插圖"""
    with GEN_SEMAPHORE:
        try:
            sess = user_sessions.setdefault(user_id, {"messages": [], "story_mode": True, "summary": "", "paras": []})
            
            # 載入或生成故事段落
            paras = load_latest_story_paragraphs(user_id) or sess.get("paras") or []
            if not paras:
                # 智能提取故事內容
                story_user_texts = [m["content"] for m in sess["messages"]
                                    if m.get("role")=="user" and not re.search(r"(幫我畫|請畫|畫|整理|總結|定妝)", m.get("content",""))]
                if story_user_texts:
                    compact_msgs = [{"role":"user","content":"\n".join(story_user_texts[-8:])}]
                    summary = generate_story_summary(compact_msgs)
                    sess["summary"] = summary
                    paras = extract_paragraphs(summary)
                    sess["paras"] = paras
                    if paras: save_story_summary(user_id, paras)

            if not paras or n >= len(paras):
                line_bot_api.push_message(user_id, TextSendMessage("資訊不足，這段再給我一些細節好嗎？"))
                return

            # 🔑 關鍵：確保角色特徵被載入
            if user_id not in user_character_cards or not user_character_cards[user_id]:
                # 從 Firebase 載入角色特徵
                loaded_character = load_character_features(user_id)
                if loaded_character:
                    user_character_cards[user_id] = loaded_character
                    print(f"🔄 已從 Firebase 載入角色特徵: {loaded_character.get('description', '')[:100]}...")
                else:
                    print("⚠️ 沒有找到已儲存的角色特徵")

            # 分析場景
            scene = analyze_scene(paras[n], user_id)
            
            # 生成 prompt - 這裡會包含角色特徵
            prompt, neg = build_image_prompt(user_id, scene, extra_desc)
            
            # 決定是否使用 Image-to-Image
            last_image = user_last_images.get(user_id, {})
            ref_id = last_image.get("image_id")
            seed = user_seeds.setdefault(user_id, random.randint(100000,999999))
            
            # 智能決定是否使用 Image-to-Image
            # 第一段不用，後續如果有基底模組就用
            use_init = bool(ref_id and n > 0 and user_character_cards.get(user_id, {}).get("base_model"))
            
            print(f"🎨 生成第 {n+1} 段插圖")
            print(f"👤 角色一致性: {get_character_consistency_prompt(user_id)[:100]}...")
            print(f"🖼️ 使用 Image-to-Image: {use_init}")
            if use_init:
                print(f"🔗 參考圖片 ID: {ref_id}")
                print(f"🔧 基底模組: {user_character_cards[user_id]['base_model']['id']}")
            
            # 📝 記錄完整的 prompt 用於調試
            print(f"📝 完整 Prompt: {prompt}")
            print(f"📝 負面 Prompt: {neg}")

            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=neg,
                seed=seed, init_image_id=(ref_id if use_init else None), 
                init_strength=(0.35 if use_init else None)  # 提高強度確保一致性
            )
            
            if result and result["url"]:
                # 更新最後一張圖片
                user_last_images[user_id] = {
                    "url": result["url"],
                    "image_id": result.get("image_id", ref_id) or ref_id
                }
                
                # 推送到 LINE
                line_bot_api.push_message(user_id, [
                    TextSendMessage(f"第 {n+1} 段完成了！"),
                    ImageSendMessage(result["url"], result["url"])
                ])
                save_chat(user_id, "assistant", f"[image]{result['url']}")
            else:
                line_bot_api.push_message(user_id, TextSendMessage("這段暫時畫不出來，再補充一點動作或場景試試？"))
                
        except Exception as e:
            print("❌ 背景生成失敗：", e)
            traceback.print_exc()
            try:
                line_bot_api.push_message(user_id, TextSendMessage("生成中遇到小狀況，等下再試一次可以嗎？"))
            except Exception:
                pass

def bg_generate_and_push_portrait(user_id):
    """背景生成角色定妝照"""
    with GEN_SEMAPHORE:
        try:
            # 使用新的角色一致性系統
            character_desc = get_character_consistency_prompt(user_id)
            seed = user_seeds.setdefault(user_id, random.randint(100000,999999))
            
            prompt = character_desc + " Beautiful, detailed character portrait. Full body shot in watercolor style."
            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt,
                negative_prompt="text, letters, words, captions, subtitles, watermark, signature, low quality, blurry",
                seed=seed
            )
            
            if result and result["url"]:
                # 更新最後一張圖片
                user_last_images[user_id] = {
                    "url": result["url"],
                    "image_id": result["image_id"]
                }
                
                # 推送到 LINE
                line_bot_api.push_message(user_id, [
                    TextSendMessage("角色定妝照完成囉～之後會以此為基準！"),
                    ImageSendMessage(result["url"], result["url"])
                ])
                save_chat(user_id, "assistant", f"[image]{result['url']}")
            else:
                line_bot_api.push_message(user_id, TextSendMessage("定妝照暫時失敗，再試一次？"))
                
        except Exception as e:
            print("❌ 背景定妝失敗：", e)
            traceback.print_exc()
            try:
                line_bot_api.push_message(user_id, TextSendMessage("定妝照遇到小狀況，等下再試一次可以嗎？"))
            except Exception:
                pass

# ---------- 啟動 ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
    
