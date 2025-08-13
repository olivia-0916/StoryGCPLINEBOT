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

# ---------- 會話 / 記憶 ----------
user_sessions       = {}  # {uid: {"messages":[...], "paras":[...], "summary":str}}
user_last_images    = {}  # {uid: {"url":..., "image_id":...}}
user_seeds          = {}  # {uid: int}
user_anchor_cards   = {}  # {uid: {...完整角色藍圖...}}

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

# ---------- 中文→英文規範化（視覺特徵） ----------
ZH2EN_COLOR = {
    "灰色":"gray","黑色":"black","白色":"white","紅色":"red","藍色":"blue","綠色":"green",
    "黃色":"yellow","粉色":"pink","紫色":"purple","橙色":"orange","棕色":"brown","咖啡色":"brown"
}
ZH2EN_HAIR = {
    "長髮":"long hair","短髮":"short hair","捲髮":"curly hair","直髮":"straight hair","馬尾":"ponytail","辮子":"braids","瀏海":"bangs"
}
ZH2EN_EYES = {
    "大眼睛":"large eyes","小眼睛":"small eyes","圓眼":"round eyes","鳳眼":"almond eyes","單眼皮":"single eyelids","雙眼皮":"double eyelids"
}
ZH2EN_BODY = {
    "高":"tall","矮":"short","胖":"chubby","瘦":"slim","壯":"muscular","嬌小":"petite","苗條":"slender"
}
ZH2EN_AGE = {
    "年輕":"young adult","老":"elderly","中年":"middle-aged","小孩":"child","大人":"adult","青少年":"teen"
}
ZH2EN_SPECIES = {
    "動物":"animal","貓":"cat","狗":"dog","鳥":"bird","魚":"fish","龍":"dragon","精靈":"elf","機器人":"robot","外星人":"alien","人類":"human"
}
ZH2EN_CLOTHING = {
    "長裙":"long skirt","短裙":"short skirt","連衣裙":"dress","百褶裙":"pleated skirt","紗裙":"tulle skirt","公主裙":"princess dress","禮服":"gown",
    "上衣":"top","襯衫":"shirt","T恤":"t-shirt","毛衣":"sweater","外套":"jacket","大衣":"coat","西裝":"suit",
    "褲子":"pants","長褲":"trousers","短褲":"shorts","牛仔褲":"jeans","休閒褲":"casual pants",
    "鞋子":"shoes","靴子":"boots","運動鞋":"sneakers","高跟鞋":"high heels","涼鞋":"sandals",
    "帽子":"hat","眼鏡":"glasses","項鍊":"necklace","手錶":"watch","包包":"bag","圍巾":"scarf","背帶褲":"suspenders"
}

def zh_lookup(token, table): return table.get(token, token)
def normalize_color_text(text):
    if not text: return text
    for zh, en in ZH2EN_COLOR.items(): text = re.sub(zh, en, text)
    return text
def normalize_piece(token):
    for tb in (ZH2EN_HAIR, ZH2EN_EYES, ZH2EN_BODY, ZH2EN_AGE, ZH2EN_SPECIES, ZH2EN_CLOTHING, ZH2EN_COLOR):
        if token in tb: return tb[token]
    return token

# ---------- 角色藍圖（Anchor Card） ----------
def parse_anchor_from_text(text):
    """
    支援快速片段：視覺/性格/行為/口頭禪/標誌物（任一或多項）
    例：
    角色設定：視覺=棕色頭髮、藍色背帶褲；性格=勇敢、好奇；行為=喜歡幫助朋友；口頭禪=出發！；標誌物=紅色小恐龍玩偶
    """
    anchor = {}
    # 視覺（自由文字也可）
    vis_m = re.search(r"(視覺|外觀|長相|穿著|外型)\s*[:=：]\s*([^\n；;]+)", text)
    if vis_m:
        vis = normalize_color_text(vis_m.group(2))
        # 嘗試把常見中文詞轉英
        tokens = re.split(r"[、,，\s]+", vis)
        vis_en = ", ".join([normalize_piece(t.strip()) for t in tokens if t.strip()])
        anchor["visual"] = vis_en or vis

    # 性格
    per_m = re.search(r"(性格|個性)\s*[:=：]\s*([^\n；;]+)", text)
    if per_m:
        anchor["personality"] = per_m.group(2).strip()

    # 行為模式
    beh_m = re.search(r"(行為|行為模式|習慣)\s*[:=：]\s*([^\n；;]+)", text)
    if beh_m:
        anchor["behavior"] = beh_m.group(2).strip()

    # 口頭禪
    catch_m = re.search(r"(口頭禪|口頭語)\s*[:=：]\s*([^\n；;]+)", text)
    if catch_m:
        anchor["catchphrase"] = catch_m.group(2).strip()

    # 標誌物/隨身物
    sig_m = re.search(r"(標誌物|隨身物|道具|物件)\s*[:=：]\s*([^\n；;]+)", text)
    if sig_m:
        anchor["signature_item"] = normalize_color_text(sig_m.group(2)).strip()

    return anchor

def autogen_anchor_from_brief(brief):
    """
    簡述 → 產生完整 Anchor Card（JSON）
    """
    sysmsg = ("你是資深兒童繪本編輯。請將使用者提供的角色簡述，補全為完整設定，"
              "包含 keys: visual(英文短語，頭髮/眼睛/穿著/顏色/特殊標記)、"
              "personality(條列詞或短語)、behavior(平常喜歡做的事/反應模式)、"
              "catchphrase(口頭禪)、signature_item(標誌物)。只輸出 JSON。")
    res = _chat([{"role":"system","content":sysmsg},{"role":"user","content":brief}], temperature=0.2)
    try:
        data = json.loads(res)
        # 視覺內詞彙正規化
        if "visual" in data and isinstance(data["visual"], str):
            toks = [t.strip() for t in re.split(r"[、,，/;；]+", data["visual"]) if t.strip()]
            data["visual"] = ", ".join([normalize_piece(normalize_color_text(t)) for t in toks])
        return data
    except Exception:
        # 後備
        return {
            "visual": "brown hair, round eyes, blue suspenders, casual outfit",
            "personality": "brave, curious, kind",
            "behavior": "helps friends, explores new places",
            "catchphrase": "Let's go!",
            "signature_item": "small red dinosaur plush"
        }

def ensure_anchor(user_id):
    """從記憶或Firestore取回；沒有就空卡"""
    if user_id in user_anchor_cards and user_anchor_cards[user_id]:
        return user_anchor_cards[user_id]
    loaded = load_anchor(user_id)
    if loaded:
        user_anchor_cards[user_id] = loaded
        return loaded
    # 初始空卡
    user_anchor_cards[user_id] = {
        "ANCHOR_ID": uuid.uuid4().hex[:6],
        "visual": "",
        "personality": "",
        "behavior": "",
        "catchphrase": "",
        "signature_item": ""
    }
    save_anchor(user_id, user_anchor_cards[user_id])
    return user_anchor_cards[user_id]

def merge_anchor(user_id, patch):
    card = ensure_anchor(user_id)
    for k,v in (patch or {}).items():
        if v: card[k] = v
    if "ANCHOR_ID" not in card or not card["ANCHOR_ID"]:
        card["ANCHOR_ID"] = uuid.uuid4().hex[:6]
    user_anchor_cards[user_id] = card
    save_anchor(user_id, card)
    return card

def anchor_text(card):
    """
    產出可重複注入的「身份證」+ 硬約束（供圖像/文字 prompt 前綴）
    """
    aid = card.get("ANCHOR_ID","????")
    visual = card.get("visual","human, long hair, large eyes, simple outfit")
    personality = card.get("personality","kind, curious")
    behavior = card.get("behavior","helps others")
    catch = card.get("catchphrase","")
    sig = card.get("signature_item","")
    # CHARACTER BIBLE + ANCHOR token（重覆兩次以強化注意力）
    base = [
        f"ANCHOR::{aid}",
        f"CHARACTER BIBLE (DO NOT CHANGE): Main character visual: {visual}.",
        "Keep face, hairstyle (length/shape), outfit items, color palette, and body proportions CONSISTENT in all images.",
        "Do NOT change age/gender/ethnicity/hairstyle/outfit/colors unless explicitly instructed.",
    ]
    if sig: base.append(f"Signature item: {sig}. Ensure it appears when appropriate.")
    # 附人格與行為（生成文字時更有幫助；圖像模型通常忽略，但保留無害）
    base += [
        f"PERSONALITY: {personality}.",
        f"BEHAVIOR: {behavior}.",
    ]
    if catch: base.append(f"CATCHPHRASE: \"{catch}\".")
    base.append(f"ANCHOR::{aid}")
    return "\n".join(base)

# ---------- 場景分析 ----------
def analyze_scene(paragraph, user_id):
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
    if re.search(r"(森林|樹林|公園|花園)", paragraph): scene["setting"] = "forest/nature"
    elif re.search(r"(城市|街道|大樓|商店)", paragraph): scene["setting"] = "urban/city"
    elif re.search(r"(家裡|房間|廚房|客廳)", paragraph): scene["setting"] = "home/indoor"
    elif re.search(r"(學校|教室|操場|圖書館)", paragraph): scene["setting"] = "school/educational"

    if re.search(r"(夜晚|晚上|深夜|月光)", paragraph): scene["time_of_day"] = "night"
    elif re.search(r"(早晨|早上|日出|黃昏)", paragraph): scene["time_of_day"] = "morning/sunset"

    if re.search(r"(快樂|開心|興奮|歡樂)", paragraph): scene["mood"] = "happy/joyful"
    elif re.search(r"(悲傷|難過|憂鬱|緊張)", paragraph): scene["mood"] = "sad/melancholy"
    elif re.search(r"(神秘|奇幻|冒險|刺激)", paragraph): scene["mood"] = "mysterious/adventurous"

    return scene

# ---------- 圖像 Prompt（內建錨定） ----------
def build_image_prompt(user_id, scene, user_extra_desc=""):
    card = ensure_anchor(user_id)
    anchor = anchor_text(card)
    style_guard = (
        "STYLE: storybook watercolor illustration, wholesome, family-friendly. "
        "COMPOSITION: full scene with environment and action; avoid plain white background; avoid isolated headshots."
    )
    scene_desc = (
        f"SCENE: setting={scene.get('setting','general location')}, "
        f"time_of_day={scene.get('time_of_day','day')}, "
        f"mood={scene.get('mood','neutral')}, "
        f"foreground action={scene.get('main_action','performing an action')}, "
        f"background={scene.get('background','environment')}, "
        f"interaction={scene.get('interaction','natural interaction')}, "
        f"key_objects={scene.get('key_objects','none')}."
    )
    parts = [anchor, style_guard, scene_desc]
    if user_extra_desc:
        parts.append(f"USER ADDITIONS: {user_extra_desc}")
    prompt = " ".join(parts)

    negative = (
        "text, letters, words, captions, subtitles, watermark, signature, "
        "plain studio white background, poster layout, close-up headshot only, "
        "different character, different face, different hairstyle, different outfit, different colors, "
        "age change, gender change, extra characters, multiple versions of the main character"
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
        "ultra": False,
        "enhancePrompt": False,
        "negative_prompt": negative_prompt,
        "seed": int(seed)
    }
    if init_image_id and init_strength is not None:
        payload["init_image_id"] = init_image_id
        payload["init_strength"] = float(init_strength)

    print("🎨 Leonardo payload =>", json.dumps(payload, ensure_ascii=False)[:1000])
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
    if not re.search(r"(叫|名|主角|角色|設定)", last_user_text):
        asks.append("先告訴我主角外觀與穿著？")
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
    user_last_images[user_id] = {}
    user_seeds[user_id] = random.randint(100000, 999999)
    # Anchor 優先載入（保留跨章記憶）
    ensure_anchor(user_id)
    print(f"✅ Reset session for {user_id}, seed={user_seeds[user_id]}")

# ---------- 背景任務 ----------
GEN_SEMAPHORE = threading.Semaphore(2)

def bg_generate_and_push_draw(user_id, n, extra_desc):
    """背景生成第 n 段插圖（先定妝→全程 i2i；每次 prompt 注入 ANCHOR）"""
    with GEN_SEMAPHORE:
        try:
            sess = user_sessions.setdefault(user_id, {"messages": [], "story_mode": True, "summary": "", "paras": []})
            # 段落
            paras = load_latest_story_paragraphs(user_id) or sess.get("paras") or []
            if not paras:
                story_user_texts = [m["content"] for m in sess["messages"]
                                    if m.get("role")=="user" and not re.search(r"(幫我畫|請畫|畫|整理|總結|定妝|角色設定|更新角色)", m.get("content",""))]
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

            scene = analyze_scene(paras[n], user_id)

            # 若沒有定妝參考，先自動定妝一次
            last_image = user_last_images.get(user_id, {})
            ref_id = last_image.get("image_id")
            seed = user_seeds.setdefault(user_id, random.randint(100000,999999))

            if not ref_id:
                portrait_prompt = anchor_text(ensure_anchor(user_id)) + \
                    " Full body character portrait, neutral pose, clear outfit and colors. Watercolor illustration."
                result0 = generate_leonardo_image(
                    user_id=user_id, prompt=portrait_prompt,
                    negative_prompt="text, letters, words, captions, subtitles, watermark, signature, plain studio background",
                    seed=seed
                )
                if result0 and result0["url"]:
                    user_last_images[user_id] = {"url": result0["url"], "image_id": result0["image_id"]}
                    ref_id = result0["image_id"]
                    try:
                        line_bot_api.push_message(user_id, TextSendMessage("先完成定妝照，接著依此一致性來畫分鏡～"))
                    except Exception:
                        pass
                else:
                    line_bot_api.push_message(user_id, TextSendMessage("定妝未成功，請再描述角色外觀或輸入「定妝」重試。"))
                    return

            # 生圖（固定 i2i，除非 extra 說換裝）
            prompt, neg = build_image_prompt(user_id, scene, extra_desc)
            use_init = True
            init_strength = 0.26
            if re.search(r"(換裝|換衣|改髮|改色|change outfit|new look)", (extra_desc or ""), flags=re.I):
                use_init = False  # 或降 0.12

            print(f"🎨 生成第 {n+1} 段 / i2i={use_init} / init_strength={init_strength if use_init else None}")
            print(f"🔗 ANCHOR 注入: {ensure_anchor(user_id).get('ANCHOR_ID')}")

            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=neg,
                seed=seed,
                init_image_id=(user_last_images[user_id]["image_id"] if use_init else None),
                init_strength=(init_strength if use_init else None)
            )

            if result and result["url"]:
                user_last_images[user_id] = {
                    "url": result["url"],
                    "image_id": result.get("image_id", user_last_images[user_id].get("image_id"))
                }
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
    """背景生成角色定妝照（Anchor Card + 全身）"""
    with GEN_SEMAPHORE:
        try:
            ensure_anchor(user_id)
            seed = user_seeds.setdefault(user_id, random.randint(100000,999999))
            prompt = anchor_text(user_anchor_cards[user_id]) + \
                     " Full body character portrait, neutral pose, clear outfit and colors. Watercolor illustration."
            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt,
                negative_prompt="text, letters, words, captions, subtitles, watermark, signature, plain studio background",
                seed=seed
            )
            if result and result["url"]:
                user_last_images[user_id] = {"url": result["url"], "image_id": result["image_id"]}
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
            msg = ("先幫主角建『角色藍圖』：\n"
                   "可直接貼：\n"
                   "角色設定：視覺=（髮色/眼色/穿著/顏色/特殊標記）；\n"
                   "性格=（內向/勇敢…）；\n"
                   "行為=（喜歡做…/遇事會…）；\n"
                   "口頭禪=（…）；\n"
                   "標誌物=（隨身物）。\n\n"
                   "或直接打一段簡述，我幫你自動補全。")
            line_bot_api.reply_message(reply_token, TextSendMessage(msg))
            return

        # 儲對話
        sess = user_sessions.setdefault(user_id, {"messages": [], "story_mode": True, "summary": "", "paras": []})
        sess["messages"].append({"role":"user","content":text})
        if len(sess["messages"]) > 60: sess["messages"] = sess["messages"][-60:]
        save_chat(user_id, "user", text)

        # 角色設定（手動全或部分）
        if text.startswith("角色設定"):
            patch = parse_anchor_from_text(text)
            if not patch:
                # 當作簡述自動補全
                brief = re.sub(r"^角色設定[:：]?\s*","",text)
                patch = autogen_anchor_from_brief(brief or "A brave, curious child in blue suspenders with a red dinosaur plush.")
            card = merge_anchor(user_id, patch)
            msg = (f"✅ 已建立/更新角色藍圖（ANCHOR {card['ANCHOR_ID']}）：\n"
                   f"視覺：{card.get('visual','')}\n性格：{card.get('personality','')}\n"
                   f"行為：{card.get('behavior','')}\n口頭禪：{card.get('catchphrase','')}\n標誌物：{card.get('signature_item','')}\n\n"
                   "輸入「定妝」可先做基準照；或直接說故事，我會在每張圖自動錨定。")
            line_bot_api.reply_message(reply_token, TextSendMessage(msg))
            return

        # 局部更新
        if text.startswith("更新角色"):
            patch = parse_anchor_from_text(text)
            if patch:
                card = merge_anchor(user_id, patch)
                line_bot_api.reply_message(reply_token, TextSendMessage(f"✅ 已更新角色藍圖（ANCHOR {card['ANCHOR_ID']}）"))
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage("請用：更新角色：性格=…；行為=…；口頭禪=…；視覺=…；標誌物=…"))
            return

        # 查看角色藍圖
        if re.search(r"(角色卡|角色藍圖|查看角色)", text):
            card = ensure_anchor(user_id)
            msg = (f"📋 角色藍圖（ANCHOR {card['ANCHOR_ID']}）\n"
                   f"視覺：{card.get('visual','')}\n性格：{card.get('personality','')}\n"
                   f"行為：{card.get('behavior','')}\n口頭禪：{card.get('catchphrase','')}\n標誌物：{card.get('signature_item','')}")
            line_bot_api.reply_message(reply_token, TextSendMessage(msg))
            return

        # 整理 / 總結（產出五段）
        if re.search(r"(整理|總結|summary)", text):
            msgs = [{"role":"system","content":base_system_prompt}] + sess["messages"][-40:]
            # 在摘要中也注入錨定，讓文本故事一致
            anchor_intro = anchor_text(ensure_anchor(user_id))
            msgs.insert(1, {"role":"user","content":"請貫徹以下角色設定與錨定：\n" + anchor_intro})
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
            ensure_anchor(user_id)
            line_bot_api.reply_message(reply_token, TextSendMessage("收到，我先做定妝照，畫好就傳給你～"))
            threading.Thread(target=bg_generate_and_push_portrait, args=(user_id,), daemon=True).start()
            return

        # 畫第 N 段
        draw_pat = r"(幫我畫第[一二三四五12345]段故事的圖|請畫第[一二三四五12345]段故事的插圖|畫第[一二三四五12345]段故事的圖)"
        if re.search(draw_pat, text):
            m = re.search(r"[一二三四五12345]", text)
            idx_map = {'一':1,'二':2,'三':3,'四':4,'五':5,'1':1,'2':2,'3':3,'4':4,'5':5}
            n = idx_map.get(m.group(0),1) - 1
            extra = re.sub(draw_pat, "", text).strip(" ，,。.!！")

            # 確保有段落
            paras = load_latest_story_paragraphs(user_id)
            if not paras:
                story_user_texts = [m["content"] for m in sess["messages"]
                                    if m.get("role")=="user" and not re.search(r"(幫我畫|請畫|畫|整理|總結|定妝|角色設定|更新角色)", m.get("content",""))]
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
