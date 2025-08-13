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

# ---------- Leonardo AI ----------
LEO_BASE  = "https://cloud.leonardo.ai/api/rest/v1"
LEO_MODEL = "7b592283-e8a7-4c5a-9ba6-d18c31f258b9"   # Lucid Origin
IMG_W = 512
IMG_H = 512

# ---------- 會話狀態 ----------
user_sessions         = {}  # {user_id: {"messages":[...], "story_mode":True, "summary":"", "paras":[...]} }
user_character_cards  = {}  # {user_id: {"物種": "貓", "髮型": "長髮", "眼型": "大眼睛", "體型": "高", "年齡": "年輕", "能力": "魔法"}}
user_story_contexts   = {}  # {user_id: "故事背景"}
user_last_images      = {}  # {user_id: {"url": "...", "image_id": "..."}}
user_seeds            = {}  # {user_id: 隨機種子值}

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

# ---------- 故事摘要生成 ----------
def generate_story_summary(messages):
    prompt = (
        "請將以下對話整理成 5 段完整故事，每段 2–3 句（約 60–120 字），"
        "每段需自然呈現場景、角色、主要動作與關鍵物件，但不要列提綱或加註。"
        "用條列 1.~5.，只輸出故事內容，不要標題、不加多餘說明。"
    )
    msgs = [{"role":"system","content":prompt}] + messages
    return _chat(msgs, temperature=0.5)

def extract_paragraphs(summary):
    if not summary: return []
    lines = [re.sub(r"^\d+\.\s*","",x.strip()) for x in summary.split("\n") if x.strip()]
    return lines[:5]

# ---------- 智能角色特徵提取 ----------
def extract_character_features(text):
    """智能提取角色特徵，支援任何類型的角色描述"""
    features = {}
    
    # 服裝特徵
    clothing_patterns = {
        "裙子": r"(長裙|短裙|連衣裙|百褶裙|紗裙|公主裙|禮服)",
        "上衣": r"(上衣|襯衫|T恤|毛衣|外套|大衣|西裝)",
        "褲子": r"(褲子|長褲|短褲|牛仔褲|休閒褲)",
        "鞋子": r"(鞋子|靴子|運動鞋|高跟鞋|涼鞋)",
        "配件": r"(帽子|眼鏡|項鍊|手錶|包包|圍巾)"
    }
    
    for category, pattern in clothing_patterns.items():
        matches = re.findall(pattern, text)
        if matches:
            features[category] = matches[0]
    
    # 顏色特徵
    color_patterns = {
        "主要顏色": r"(灰色|黑色|白色|紅色|藍色|綠色|黃色|粉色|紫色|橙色|棕色)",
        "服裝顏色": r"(穿|戴|著)(灰色|黑色|白色|紅色|藍色|綠色|黃色|粉色|紫色|橙色|棕色)",
        "頭髮顏色": r"(頭髮|髮色)(是|為|為|的)(灰色|黑色|白色|紅色|藍色|綠色|黃色|粉色|紫色|橙色|棕色)"
    }
    
    for category, pattern in color_patterns.items():
        matches = re.findall(pattern, text)
        if matches:
            features[category] = matches[0]
    
    # 外貌特徵
    if re.search(r"(長髮|短髮|捲髮|直髮|馬尾|辮子)", text):
        features["髮型"] = re.search(r"(長髮|短髮|捲髮|直髮|馬尾|辮子)", text).group(1)
    
    if re.search(r"(大眼睛|小眼睛|圓眼|鳳眼|單眼皮|雙眼皮)", text):
        features["眼型"] = re.search(r"(大眼睛|小眼睛|圓眼|鳳眼|單眼皮|雙眼皮)", text).group(1)
    
    if re.search(r"(高|矮|胖|瘦|壯|嬌小|苗條)", text):
        features["體型"] = re.search(r"(高|矮|胖|瘦|壯|嬌小|苗條)", text).group(1)
    
    if re.search(r"(年輕|老|中年|小孩|大人|青少年)", text):
        features["年齡"] = re.search(r"(年輕|老|中年|小孩|大人|青少年)", text).group(1)
    
    # 特殊特徵
    if re.search(r"(動物|貓|狗|鳥|魚|龍|精靈|機器人|外星人)", text):
        features["物種"] = re.search(r"(動物|貓|狗|鳥|魚|龍|精靈|機器人|外星人)", text).group(1)
    
    if re.search(r"(魔法|超能力|特殊能力|技能)", text):
        features["能力"] = re.search(r"(魔法|超能力|特殊能力|技能)", text).group(1)
    
    return features

def update_character_card(user_id, text):
    """動態更新角色卡，支援任何類型的角色"""
    if user_id not in user_character_cards:
        user_character_cards[user_id] = {}
    
    # 提取新特徵
    new_features = extract_character_features(text)
    
    # 更新角色卡
    if new_features:
        user_character_cards[user_id].update(new_features)
        
        # 生成角色描述
        character_desc = build_character_description(user_character_cards[user_id])
        user_character_cards[user_id]["description"] = character_desc
        
        print(f"✨ 角色卡已更新: {character_desc[:100]}...")
        return True
    
    return False

def build_character_description(features):
    """根據特徵建立角色描述"""
    parts = []
    
    # 基本描述
    if "物種" in features:
        parts.append(f"A {features['物種']}")
    else:
        parts.append("A person")
    
    # 外貌特徵
    if "年齡" in features:
        parts.append(f"who is {features['年齡']}")
    
    if "體型" in features:
        parts.append(f"with a {features['體型']} build")
    
    if "髮型" in features:
        parts.append(f"having {features['髮型']}")
    
    if "眼型" in features:
        parts.append(f"with {features['眼型']}")
    
    # 服裝特徵
    clothing_parts = []
    if "裙子" in features:
        clothing_parts.append(f"wearing a {features['裙子']}")
    if "上衣" in features:
        clothing_parts.append(f"in a {features['上衣']}")
    if "褲子" in features:
        clothing_parts.append(f"with {features['褲子']}")
    
    if clothing_parts:
        parts.append(", ".join(clothing_parts))
    
    # 顏色特徵
    if "主要顏色" in features:
        parts.append(f"in {features['主要顏色']} color")
    
    # 能力特徵
    if "能力" in features:
        parts.append(f"with {features['能力']}")
    
    # 組合描述
    description = " ".join(parts) + "."
    
    # 添加一致性要求
    description += " Maintain consistent appearance across all images: same face, hairstyle, clothing, colors, and proportions."
    
    return description

def get_character_prompt(user_id):
    """獲取角色 prompt"""
    if user_id in user_character_cards and "description" in user_character_cards[user_id]:
        return user_character_cards[user_id]["description"]
    return "Main character with unique features. Maintain consistent appearance across all images."

# ---------- 場景分析 ----------
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

# ---------- 圖像 Prompt 生成 ----------
def build_image_prompt(user_id, scene, user_extra_desc=""):
    """生成開放的圖像 prompt，支援任何類型的角色和場景"""
    
    # 基礎角色描述
    character_base = get_character_prompt(user_id)
    
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
        "age change, gender change, inconsistent appearance"
    )
    
    return prompt, negative

# ---------- Leonardo AI ----------
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

def leonardo_poll(gen_id, timeout=150):
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
                    generated_images = generation_data.get("generated_images", [])
                    if generated_images:
                        gi = generated_images[0]
                        return gi.get("url"), gi.get("id")
                    else:
                        print("❌ 沒有找到生成的圖片")
                        return None, None
                elif status == "FAILED":
                    print("❌ 圖片生成失敗")
                    return None, None
            else:
                print(f"⚠️ 回應格式異常: {data}")
                
        except Exception as e:
            print(f"❌ 檢查狀態時發生錯誤: {e}")
            traceback.print_exc()
            
    print(f"⏰ 輪詢超時 ({timeout}s)，生成 ID: {gen_id}")
    return None, None

def generate_leonardo_image(*, user_id, prompt, negative_prompt, seed, init_image_id=None, init_strength=None):
    payload = {
        "modelId": LEO_MODEL,
        "prompt": prompt[:1500],
        "num_images": 1,
        "width": IMG_W, "height": IMG_H,
        "contrast": 3.0,
        "ultra": False,
        "enhancePrompt": False,
        "negative_prompt": negative_prompt,
        "seed": int(seed)
    }
    
    # Image-to-Image 參數
    if init_image_id and init_strength:
        payload["init_image_id"] = init_image_id
        payload["init_strength"] = float(init_strength)

    print("🎨 Leonardo payload =>", json.dumps(payload, ensure_ascii=False))
    
    try:
        gen_id = leonardo_tti(payload)
        print("✅ Leonardo Generation ID:", gen_id)
        
        url, image_id = leonardo_poll(gen_id)
        if url:
            gcs_url = upload_to_gcs_from_url(url, user_id, prompt)
            return {"url": gcs_url, "image_id": image_id}
            
    except requests.HTTPError as e:
        if init_image_id and "Unexpected variable" in str(e):
            print("↩️ 自動降級：改用 text-to-image 重試")
            return generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=negative_prompt,
                seed=seed, init_image_id=None, init_strength=None
            )
        print("❌ Leonardo HTTP 錯誤：", e)
    except Exception as e:
        print(f"❌ Leonardo 其他錯誤：{e}")
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
    brief = last_user_text if len(last_user_text) <= 40 else last_user_text[:40] + "…"
    asks = []
    
    # 開放式引導，不預設任何特定內容
    if not re.search(r"(叫|名|主角|角色)", last_user_text):
        asks.append("主角或角色是什麼呢？")
    if not re.search(r"(在哪|哪裡|什麼地方|場景)", last_user_text):
        asks.append("這段發生在哪裡呢？")
    if not re.search(r"(做什麼|發生|遇到|準備|解決)", last_user_text):
        asks.append("這段想發生什麼事情呢？")
    
    if not asks: asks = ["想再加哪個小細節？"]
    return f"我聽到了：{brief}\n很有畫面感！\n{asks[0]}"

# ---------- Flask 路由 ----------
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
    user_character_cards[user_id] = {} # 重置角色卡
    user_story_contexts[user_id] = {}
    user_seeds[user_id] = random.randint(100000, 999999)
    print(f"✅ Reset session for {user_id}, seed={user_seeds[user_id]}")

# ---------- 背景任務 ----------
GEN_SEMAPHORE = threading.Semaphore(2)

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

            # 分析場景
            scene = analyze_scene(paras[n], user_id)
            
            # 生成 prompt
            prompt, neg = build_image_prompt(user_id, scene, extra_desc)
            
            # 決定是否使用 Image-to-Image
            last_image = user_last_images.get(user_id, {})
            ref_id = last_image.get("image_id")
            seed = user_seeds.setdefault(user_id, random.randint(100000,999999))
            
            # 智能決定是否使用 Image-to-Image
            # 第一段不用，後續如果有角色卡且不是第一次畫圖就用
            use_init = bool(ref_id and n > 0 and user_character_cards.get(user_id, {}).get("description"))
            
            print(f"🎨 生成第 {n+1} 段插圖")
            print(f"👤 角色卡: {get_character_prompt(user_id)[:100]}...")
            print(f"🖼️ 使用 Image-to-Image: {use_init}")
            if use_init:
                print(f"🔗 參考圖片 ID: {ref_id}")

            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=neg,
                seed=seed, init_image_id=(ref_id if use_init else None), 
                init_strength=(0.3 if use_init else None)  # 提高強度確保一致性
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
            # 使用現有角色卡或建立基礎角色卡
            character_desc = get_character_prompt(user_id)
            seed = user_seeds.setdefault(user_id, random.randint(100000,999999))
            
            prompt = character_desc + " Beautiful, detailed character portrait. Full body shot."
            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt,
                negative_prompt="text, letters, words, captions, subtitles, watermark, signature",
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
            line_bot_api.reply_message(reply_token, TextSendMessage("太好了！先說主角與地點吧？"))
            return

        sess = user_sessions.setdefault(user_id, {"messages": [], "story_mode": True, "summary": "", "paras": []})
        sess["messages"].append({"role":"user","content":text})
        if len(sess["messages"]) > 60: sess["messages"] = sess["messages"][-60:]
        save_chat(user_id, "user", text)

        # 智能角色特徵提取和更新
        if update_character_card(user_id, text):
            print(f"✨ 角色卡已更新: {user_character_cards[user_id]['description'][:100]}...")

        # 整理 / 總結
        if re.search(r"(整理|總結|summary)", text):
            msgs = [{"role":"system","content":base_system_prompt}] + sess["messages"][-40:]
            summary = generate_story_summary(msgs)
            sess["summary"] = summary
            paras = extract_paragraphs(summary)
            sess["paras"] = paras
            if paras:
                save_story_summary(user_id, paras)
                clean = "\n".join([f"{i+1}. {p}" for i,p in enumerate(paras)])
                line_bot_api.reply_message(reply_token, TextSendMessage(clean))
                save_chat(user_id, "assistant", clean)
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage("資訊還不夠，我們再補一些細節吧～"))
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
                    if key != "description":
                        response += f"• {key}: {value}\n"
                if "description" in character_info:
                    response += f"\n🎨 完整描述：\n{character_info['description']}"
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

            line_bot_api.reply_message(reply_token, TextSendMessage(f"收到，我開始畫第 {n+1} 段，完成就傳給你～"))
            threading.Thread(target=bg_generate_and_push_draw, args=(user_id,n,extra), daemon=True).start()
            return

        # 一般引導
        sysmsg = base_system_prompt
        msgs = [{"role":"system","content":sysmsg}] + sess["messages"][-12:]
        reply = _chat(msgs, temperature=0.7) or natural_guidance(text)
        reply = format_reply(reply)
        line_bot_api.reply_message(reply_token, TextSendMessage(reply))
        save_chat(user_id, "assistant", reply)

    except Exception as e:
        print("❌ 發生錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage("小繪出了一點小狀況，稍後再試 🙇"))

# ---------- 啟動 ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
