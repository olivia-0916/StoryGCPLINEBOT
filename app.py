# app.py
import os, sys, json, time, uuid, re, random, traceback, base64
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import requests

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import storage as gcs_storage

# ----------------------------
# 基本設定
# ----------------------------
sys.stdout.reconfigure(encoding="utf-8")
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET      = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY           = os.environ.get("OPENAI_API_KEY")          # gpt-4o-mini
LEONARDO_API_KEY         = os.environ.get("LEONARDO_API_KEY")        # Leonardo REST
FIREBASE_CREDENTIALS     = os.environ.get("FIREBASE_CREDENTIALS")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

def _firebase_creds():
    return credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
firebase_admin.initialize_app(_firebase_creds())
db = firestore.client()

# GCS
GCS_BUCKET = "storybotimage"
gcs_client = gcs_storage.Client()
gcs_bucket = gcs_client.bucket(GCS_BUCKET)

# Leonardo
LEO_BASE = "https://cloud.leonardo.ai/api/rest/v1"
LEO_MODEL = "7b592283-e8a7-4c5a-9ba6-d18c31f258b9"  # Lucid Origin（品質/速度平衡）
IMG_W = 512
IMG_H = 512

# ----------------------------
# 會話狀態
# ----------------------------
user_sessions = {}  # {user_id: {...}}
# 保持一致性的核心資料
user_fixed_seed       = {}  # 固定 seed
user_character_sheet  = {}  # 主角設定卡（文字前綴）
user_definitive_imgid = {}  # 定妝照 image_id
user_definitive_url   = {}  # 定妝照 url
user_world_state      = {}  # 世界觀（setting/time/mood/palette）
user_scene_briefs     = {}  # 每段場景摘要（帶動作/互動）

# ----------------------------
# OpenAI 簡易呼叫
# ----------------------------
def _chat(messages, temperature=0.6):
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

# ----------------------------
# Firebase / GCS
# ----------------------------
def save_chat(user_id, role, text):
    try:
        db.collection("users").document(user_id).collection("chat").add({
            "role": role, "text": text, "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print("⚠️ Firebase save_chat failed:", e)

def upload_to_gcs_from_url(url, user_id, prompt):
    try:
        r = requests.get(url)
        r.raise_for_status()
        fname = f"{user_id}_{uuid.uuid4().hex}.png"
        blob = gcs_bucket.blob(fname)
        blob.upload_from_string(r.content, content_type="image/png")
        gcs_url = f"https://storage.googleapis.com/{GCS_BUCKET}/{fname}"
        db.collection("users").document(user_id).collection("images").add({
            "url": gcs_url, "prompt": prompt, "timestamp": firestore.SERVER_TIMESTAMP
        })
        print("✅ 圖片已上傳至 GCS 並儲存：", gcs_url)
        return gcs_url
    except Exception as e:
        print("❌ GCS upload failed:", e)
        return None

# ----------------------------
# 故事整理（加長 & 帶關鍵元素）
# ----------------------------
def generate_story_summary(messages):
    prompt = (
        "請將以下對話整理成 5 段完整故事，每段 2–3 句（約 60–120 字），"
        "每段必須涵蓋：場景(地點/時間/氛圍)、出現角色(含主角)、主要動作、關鍵物件或互動。"
        "用條列 1.~5.，僅輸出故事內容，不要加標題或多餘說明。"
    )
    msgs = [{"role":"system","content":prompt}] + messages
    res = _chat(msgs, temperature=0.5)
    return res

def extract_paragraphs(summary):
    if not summary: return []
    lines = [re.sub(r"^\d+\.\s*","",x.strip()) for x in summary.split("\n") if x.strip()]
    # 只取前 5 段
    return lines[:5]

# ----------------------------
# 從段落產出「動態敘事場景 brief」
# ----------------------------
def build_scene_brief(paragraph, world_hint=None):
    sysmsg = (
        "你是資深繪本分鏡師。從段落提煉【場景、時間、氛圍、前景/背景重點、主角動作/情緒、與物/人的互動、關鍵物件】，"
        "輸出 JSON（keys: setting, time_of_day, mood, foreground, background, main_action, interaction, key_objects）。"
        "若段落未明確地點，承襲 world_hint.setting。若未提時間/氛圍，也承襲 world_hint。所有值用簡短英文片語。"
    )
    user = f"段落：{paragraph}\nworld_hint：{json.dumps(world_hint or {}, ensure_ascii=False)}"
    res = _chat([{"role":"system","content":sysmsg},{"role":"user","content":user}], temperature=0.2)
    try:
        data = json.loads(res)
        # 填補缺省
        data.setdefault("setting", (world_hint or {}).get("setting","forest"))
        data.setdefault("time_of_day",(world_hint or {}).get("time_of_day","day"))
        data.setdefault("mood",(world_hint or {}).get("mood","calm"))
        data.setdefault("foreground","main character performing the action")
        data.setdefault("background","environmental elements supporting story")
        data.setdefault("main_action","walking")
        data.setdefault("interaction","natural interaction with objects or people")
        data.setdefault("key_objects","")
        return data
    except Exception:
        # fallback
        return {
            "setting": (world_hint or {}).get("setting","forest"),
            "time_of_day": (world_hint or {}).get("time_of_day","day"),
            "mood": (world_hint or {}).get("mood","calm"),
            "foreground": "main character in action",
            "background": "environment details",
            "main_action": "walking",
            "interaction": "looking / pointing / holding",
            "key_objects": ""
        }

# ----------------------------
# 圖像 Prompt：主角一致性 + 動態敘事
# ----------------------------
def build_image_prompt(user_id, scene_brief, user_extra_desc=""):
    # 一致性（默認東亞臉孔 + 穿著/標誌物）
    character = user_character_sheet.get(user_id) or (
        "Consistent main character across all images. Same face, hairstyle, clothing, colors, proportions. "
        "Whimsical watercolor storybook style. Primary ethnicity: East Asian features; black hair, dark brown eyes, warm fair skin. "
        "If user does not specify otherwise, keep East Asian facial structure and same hairstyle. "
        "Signature outfit/items must appear on the main character only."
    )

    world = user_world_state.get(user_id) or {
        "setting": "forest",
        "time_of_day": "day",
        "mood": "calm",
        "palette": "soft watercolor palette, greens and warm light"
    }

    # 場景描述（動作/互動）
    parts = [
        character,
        "family-friendly, wholesome, uplifting tone, modest clothing, safe for work, non-violent.",
        "Full-scene composition; avoid centered portrait; show environment and story action.",
        f"Scene description: setting: {scene_brief.get('setting', world['setting'])}, "
        f"time of day: {scene_brief.get('time_of_day', world['time_of_day'])}, "
        f"mood: {scene_brief.get('mood', world['mood'])}, ",
        f"foreground: {scene_brief.get('foreground','')}, ",
        f"background: {scene_brief.get('background','')}, ",
        f"main character action: {scene_brief.get('main_action','')}, ",
        f"interaction: {scene_brief.get('interaction','')}, ",
        f"key objects: {scene_brief.get('key_objects','')}.",
    ]
    if user_extra_desc:
        parts.append(f"User additions: {user_extra_desc}")

    prompt = " ".join(parts)
    neg = (
        "text, letters, words, captions, subtitles, watermark, signature, "
        "different character, change hairstyle, change outfit, age change, gender change, "
        "blonde hair, red hair, light brown hair, blue eyes, green eyes, non-East-Asian facial features"
    )
    return prompt, neg

# ----------------------------
# Leonardo API
# ----------------------------
def leonardo_headers():
    return {"Authorization": f"Bearer {LEONARDO_API_KEY.strip()}",
            "Accept": "application/json", "Content-Type": "application/json"}

def leonardo_tti(payload):  # text-to-image
    url = f"{LEO_BASE}/generations"
    r = requests.post(url, headers=leonardo_headers(), json=payload, timeout=45)
    if not r.ok:
        print("❌ Leonardo POST 失敗:", r.status_code, r.text)
    r.raise_for_status()
    data = r.json()
    return data["sdGenerationJob"]["generationId"]

def leonardo_poll(gen_id, timeout=120):
    url = f"{LEO_BASE}/generations/{gen_id}"
    start = time.time()
    while time.time()-start < timeout:
        time.sleep(4)
        r = requests.get(url, headers=leonardo_headers(), timeout=30)
        if not r.ok:
            print("❌ Leonardo GET 失敗:", r.status_code, r.text)
        r.raise_for_status()
        data = r.json()
        if data.get("generations_v2") and data["generations_v2"][0]["status"] == "COMPLETE":
            gi = data["generations_v2"][0]["generated_images"][0]
            return gi.get("url"), gi.get("id")
    return None, None

def generate_leonardo_image(*, user_id, prompt, negative_prompt, seed, init_image_id=None, init_strength=None):
    # 注意：img2img 正確欄位為 init_generation_image_id + init_strength
    payload = {
        "modelId": LEO_MODEL,
        "prompt": prompt[:1500],    # 長度保護
        "num_images": 1,
        "width": IMG_W, "height": IMG_H,
        "contrast": 3.0,
        "ultra": False,
        "enhancePrompt": False,
        "negative_prompt": negative_prompt,
        "seed": int(seed)
    }

    if init_image_id and init_strength:
        payload["isInitImage"] = True
        payload["init_generation_image_id"] = init_image_id
        payload["init_strength"] = float(init_strength)  # ✅ 正確 key

    print("🎨 Leonardo payload =>", json.dumps(payload, ensure_ascii=False))
    try:
        gen_id = leonardo_tti(payload)
        print("✅ Leonardo Generation ID:", gen_id)
        url, image_id = leonardo_poll(gen_id)
        if url:
            gcs_url = upload_to_gcs_from_url(url, user_id, prompt)
            return {"url": gcs_url, "image_id": image_id}
    except requests.HTTPError as e:
        # 若 img2img 參數不被接受 → 自動降級成 TTI
        if init_image_id and "Unexpected variable" in str(e):
            print("↩️ 自動降級：改用 text-to-image 重試（保留 seed 與 prompt）")
            return generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=negative_prompt,
                seed=seed, init_image_id=None, init_strength=None
            )
        print("❌ Leonardo 例外：", e)
    return None

# ----------------------------
# 引導系統提示
# ----------------------------
base_system_prompt = (
    "你是「小繪」，一位親切、溫柔、擅長說故事的 AI 夥伴，協助長輩創作 5 段故事繪本。\n"
    "請用簡潔、好讀的語氣回應；每則訊息盡量不超過 35 字並適當分段。\n"
    "第一階段：依「如果我有一個超能力」主題，循序引導主角/地點/事件/解法。\n"
    "第二階段：協助描述每段畫面（不要把文字畫在圖上）。\n"
    "請自稱「小繪」。"
)

def format_reply(text):
    return re.sub(r'([。！？])\s*', r'\1\n', text)

# ----------------------------
# Flask 路由
# ----------------------------
@app.route("/")
def root():
    return "LINE GPT Webhook is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    if not signature:
        print("⚠️ Missing X-Line-Signature — non-LINE request (axios/Postman/healthcheck?). Ignored.")
        return "OK"
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ----------------------------
# 狀態工具
# ----------------------------
def reset_session(user_id):
    user_sessions[user_id] = {"messages": [], "story_mode": True}
    user_fixed_seed[user_id] = random.randint(100000, 999999)
    user_world_state[user_id] = {"setting":"forest","time_of_day":"day","mood":"calm",
                                 "palette":"soft watercolor palette, greens and warm light"}
    user_scene_briefs[user_id] = []
    print(f"✅ Reset session for {user_id}, seed={user_fixed_seed[user_id]}")

# ----------------------------
# 主處理
# ----------------------------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()
    reply_token = event.reply_token
    print(f"📩 {user_id}：{user_text}")

    # 允許健康檢查時無 signer
    if not LINE_CHANNEL_SECRET:
        print("⚠️ LINE_CHANNEL_SECRET 未設，請確認環境變數。")

    try:
        # 啟動
        if re.search(r"(開始說故事|說故事|講個故事|一起來講故事吧|我們來講故事吧)", user_text):
            reset_session(user_id)
            line_bot_api.reply_message(reply_token, TextSendMessage("太好了，我們開始吧！主角是誰？在哪裡？"))
            return

        # 累積對話
        sess = user_sessions.setdefault(user_id, {"messages": [], "story_mode": True})
        sess["messages"].append({"role":"user","content":user_text})
        save_chat(user_id, "user", user_text)

        # 使用者指定主角裝扮（更新設定卡）
        if re.search(r"(穿|戴|頭上|衣|裙|襯衫|鞋|配件)", user_text):
            addon = re.sub(r"^[^，。]*?(穿|戴)", "wears ", user_text)
            user_character_sheet[user_id] = (
                "Consistent main character across all images. Same face, hairstyle, clothing, colors, proportions. "
                "Whimsical watercolor storybook style. Primary ethnicity: East Asian features; black hair, dark brown eyes, warm fair skin. "
                f"Main character always wears/has: {addon}. Only the main character has these signature items."
            )
            print("✨ 角色設定卡已更新:", user_character_sheet[user_id])

        # 產生/更新故事摘要（較長 & 有元素）
        if re.search(r"(整理|總結|summary)", user_text):
            full = [{"role":"system","content":base_system_prompt}] + sess["messages"][-40:]
            summary = generate_story_summary(full)
            sess["summary"] = summary
            paras = extract_paragraphs(summary)
            # 建立每段的 scene brief
            world = user_world_state.get(user_id)
            briefs = []
            for p in paras:
                b = build_scene_brief(p, world)
                briefs.append(b)
                # 世界觀以第 1 段為主（若有更明確再覆蓋）
                user_world_state[user_id] = {
                    "setting": b.get("setting", world["setting"]),
                    "time_of_day": b.get("time_of_day", world["time_of_day"]),
                    "mood": b.get("mood", world["mood"]),
                    "palette": world.get("palette","soft watercolor palette")
                }
            user_scene_briefs[user_id] = briefs

            # 回覆更長的整理
            pretty = []
            for i, p in enumerate(paras, 1):
                b = briefs[i-1]
                pretty.append(
                    f"{i}. {p}\n"
                    f"   場景：{b['setting']}｜時間：{b['time_of_day']}｜氛圍：{b['mood']}\n"
                    f"   重點：主角動作 {b['main_action']}；互動 {b['interaction']}；物件 {b['key_objects'] or '—'}"
                )
            line_bot_api.reply_message(reply_token, TextSendMessage("\n\n".join(pretty)))
            save_chat(user_id, "assistant", "\n\n".join(prety for prety in pretty))
            return

        # 生成第一張定妝照（若尚未有）
        if "定妝" in user_text or (user_definitive_imgid.get(user_id) is None and re.search(r"第一段", user_text)):
            if user_character_sheet.get(user_id) is None:
                user_character_sheet[user_id] = (
                    "Consistent main character across all images. Same face, hairstyle, clothing, colors, proportions. "
                    "Whimsical watercolor storybook style. Primary ethnicity: East Asian features; black hair, dark brown eyes, warm fair skin. "
                    "Signature outfit/items must appear on the main character only."
                )
            seed = user_fixed_seed.setdefault(user_id, random.randint(100000,999999))
            prompt = user_character_sheet[user_id] + " family-friendly, wholesome, uplifting tone, modest clothing, safe for work, non-violent."
            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt="text, letters, words, captions, subtitles, watermark, signature",
                seed=seed, init_image_id=None, init_strength=None
            )
            if result and result["url"]:
                user_definitive_imgid[user_id] = result["image_id"]
                user_definitive_url[user_id]   = result["url"]
                line_bot_api.reply_message(reply_token, [
                    TextSendMessage("這是主角的定妝照～之後會以此為基準喔"),
                    ImageSendMessage(result["url"], result["url"])
                ])
                save_chat(user_id, "assistant", f"[image]{result['url']}")
                return

        # 畫第 N 段故事
        if re.search(r"第[一二三四五12345]段", user_text):
            m = re.search(r"[一二三四五12345]", user_text)
            idx_map = {'一':1,'二':2,'三':3,'四':4,'五':5,'1':1,'2':2,'3':3,'4':4,'5':5}
            n = idx_map.get(m.group(0),1) - 1

            # 若還沒有摘要，先做一次
            if not user_scene_briefs.get(user_id):
                full = [{"role":"system","content":base_system_prompt}] + sess["messages"][-40:]
                summary = generate_story_summary(full)
                sess["summary"] = summary
                paras = extract_paragraphs(summary)
                briefs = [build_scene_brief(p, user_world_state.get(user_id)) for p in paras]
                user_scene_briefs[user_id] = briefs

            briefs = user_scene_briefs.get(user_id, [])
            if not briefs or n >= len(briefs):
                line_bot_api.reply_message(reply_token, TextSendMessage("小繪還沒整理好這段，再給我一點線索～"))
                return

            scene = briefs[n]
            # 取使用者附加描述（句尾補充）
            extra = re.sub(r".*段故事的圖", "", user_text).strip(" ，,。.!！")
            prompt, neg = build_image_prompt(user_id, scene, extra)

            # 若有定妝照 → img2img 低強度
            ref_id = user_definitive_imgid.get(user_id)
            seed   = user_fixed_seed.setdefault(user_id, random.randint(100000,999999))
            result = generate_leonardo_image(
                user_id=user_id, prompt=prompt, negative_prompt=neg,
                seed=seed,
                init_image_id=ref_id, init_strength=0.24 if ref_id else None
            )

            if result and result["url"]:
                user_definitive_imgid[user_id] = result.get("image_id", ref_id) or ref_id
                user_definitive_url[user_id]   = result["url"]
                line_bot_api.reply_message(reply_token, [
                    TextSendMessage(f"這是第 {n+1} 段的插圖："),
                    ImageSendMessage(result["url"], result["url"])
                ])
                save_chat(user_id,"assistant",f"[image]{result['url']}")
                return
            else:
                line_bot_api.reply_message(reply_token, TextSendMessage("這段畫不出來，我再微調一下，你可再描述多一點動作或情緒嗎？"))
                return

        # 一般對話 → 繼續引導創作
        sysmsg = base_system_prompt
        # 附帶目前摘要，利於延續
        summary = user_sessions[user_id].get("summary","")
        if summary:
            sysmsg += f"\n【故事摘要】\n{summary}\n請延續互動。"
        msgs = [{"role":"system","content":sysmsg}] + sess["messages"][-20:]
        reply = _chat(msgs, temperature=0.7) or "好喔，我們繼續～"
        reply = format_reply(reply)
        line_bot_api.reply_message(reply_token, TextSendMessage(reply))
        save_chat(user_id, "assistant", reply)

    except Exception as e:
        print("❌ 發生錯誤：", e)
        traceback.print_exc()
        line_bot_api.reply_message(reply_token, TextSendMessage("小繪出了一點小狀況，稍後再試 🙇"))

# ----------------------------
# 啟動
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
