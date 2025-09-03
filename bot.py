# -*- coding: utf-8 -*-
"""
بوت تيليجرام: تحميل فيديوهات/صور من أغلب مواقع التواصل (يوتيوب، إنستغرام، تيك توك، فيسبوك، تويتر...)
مصمم ليعمل مجانًا على خدمات مثل Render بوضع "الخمول/الاستيقاظ" تلقائيًا عبر الـ Webhook.

- لا يعتمد على ffmpeg (نختار صيغة جاهزة قدر الإمكان لتفادي الدمج).
- تعليقات عربية واضحة على جميع الأجزاء.
- يعمل محليًا بـ polling، وعلى Render بـ webhook (للاستيقاظ عند أول رسالة).
- يرجى استخدامه للمحتوى المسموح به فقط واحترام حقوق النشر وشروط المواقع.

طريقة التشغيل المحلية:
1) ثبت المتطلبات: pip install -r requirements.txt
2) ضع متغير البيئة BOT_TOKEN=توكن_البوت
3) شغّل: python bot.py
4) أرسل رابط لأي فيديو/صورة للبوت.

طريقة النشر على Render (ملخص):
- أول نشر: اربط المستودع وشغّل (سيظهر لك رابط الخدمة).
- انسخ رابط الخدمة وضعه في متغير APP_URL ثم أعد النشر (حتى يتم تفعيل الـ webhook).
"""

import os
import re
import asyncio
import logging
import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import List

from telegram import Update, InputFile
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

import yt_dlp

# ===================== إعدادات عامة =====================

# قراءة المتغيرات من البيئة (ضعها في Render أو محليًا)
BOT_TOKEN = os.getenv("7574777696:AAEGdnY_RK0lPEQPxsvPSm8E7VSd8fQPO-w")  # توكن البوت من @BotFather (إلزامي)
APP_URL   = os.getenv("https://downloader-1-q7bv.onrender.com")    # مثال: https://your-app.onrender.com (اختياري محليًا، إلزامي على Render)
PORT      = int(os.getenv("PORT", "8080"))  # Render يمرر المنفذ تلقائيًا

# مجلد مؤقت لحفظ التنزيلات، يُنظَّف بعد الإرسال
BASE_DOWNLOAD_DIR = Path("downloads")

# تفعيل السجلّات للمساعدة على تتبّع الأخطاء
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ===================== أدوات مساعدة =====================

# نمط بسيط للتحقق أن النص يحتوي على عنوان URL
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

def ensure_dir(p: Path) -> None:
    """إنشاء المجلد إن لم يكن موجودًا."""
    p.mkdir(parents=True, exist_ok=True)

def cleanup_dir(p: Path) -> None:
    """حذف المجلد بأكمله بشكل آمن."""
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)

def pick_send_method(filepath: Path) -> str:
    """
    تحديد طريقة الإرسال الأنسب بناءً على نوع الملف:
    - صور → sendPhoto
    - فيديو → sendVideo
    - غير ذلك → sendDocument
    """
    mime, _ = mimetypes.guess_type(str(filepath))
    if mime:
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("video/"):
            return "video"
    return "document"


async def send_action_typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إظهار حالة الكتابة لتحسين تجربة المستخدم."""
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass


# ===================== التنزيل عبر yt-dlp =====================

def _ytdlp_opts(outtmpl: str) -> dict:
    """
    خيارات yt-dlp مضبوطة لتفادي الاعتماد على ffmpeg قدر الإمكان:
    - نفضّل ملفات MP4 الجاهزة (صوت+فيديو في ملف واحد) حتى لا نحتاج دمج.
    - إن لم تتوفر، نختار أفضل ملف متاح (قد يفشل أحيانًا إن تطلب دمجًا).
    """
    return {
        # اسم الملف الناتج: العنوان-المعرف.الامتداد داخل مجلد المستخدم المؤقت
        "outtmpl": outtmpl,
        # لا تنزّل قوائم تشغيل/بلاي ليست كاملة
        "noplaylist": True,
        # حاول إعادة المحاولة عند الفشل المؤقت
        "retries": 3,
        "continuedl": True,
        # هدوء في الإخراج (السجلات تظهر فقط إذا INFO/ERROR)
        "quiet": True,
        "no_warnings": True,
        # تفضيل ملف جاهز (بدون دمج) قدر الإمكان
        # - أولاً: فيديو mp4 بصوت وفيديو جاهز
        # - ثانياً: أفضل ملف mp4 منفرد
        # - ثالثاً: أي ملف متاح كحل أخير
        "format": "bv*[ext=mp4][acodec!=none]/b[ext=mp4]/b/best",
        # عدم إجبار استخدام ffmpeg
        "prefer_ffmpeg": False,
        # تسريع تنزيل المقاطع المقطّعة عند الإمكان
        "concurrent_fragment_downloads": 4,
        # إضافة وصف مقتضب داخل الميتاداتا إن توفّر
        "writedescription": False,
    }

def download_with_ytdlp(url: str, download_dir: Path) -> List[Path]:
    """
    تنزيل الوسائط من رابط واحد باستخدام yt-dlp.
    يعيد قائمة بالملفات الناتجة (صور/فيديوهات ...).
    """
    ensure_dir(download_dir)
    # قالب اسم الملف داخل مجلد المستخدم
    outtmpl = str(download_dir / "%(title).80s-%(id)s.%(ext)s")

    ydl_opts = _ytdlp_opts(outtmpl)
    files: List[Path] = []

    # نستخدم extract_info للحصول على المسارات النهائية للملفات
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)  # يقوم بالتنزيل ثم يعطينا معلومات
        # قد يُعيد ydl عدة عناصر حسب المنصة/المنشور
        # requested_downloads تحمل المسارات النهائية للملفات المنزّلة
        req = info.get("requested_downloads") or []
        for item in req:
            fp = item.get("filepath")
            if fp:
                files.append(Path(fp))

        # احتياط: إن لم نجد شيء في requested_downloads نحاول اكتشاف الملفات داخل المجلد
        if not files:
            for p in download_dir.glob("*"):
                if p.is_file():
                    files.append(p)

    # إزالة التكرار إن وجد
    unique = []
    seen = set()
    for f in files:
        if f.exists() and str(f) not in seen:
            unique.append(f)
            seen.add(str(f))
    return unique


# ===================== الأوامر (start/help/about) =====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """رسالة الترحيب والأوامر الأساسية."""
    text = (
        "أهلًا بك 👋\n\n"
        "أنا بوت تحميل وسائط 📥 من أغلب مواقع التواصل (يوتيوب، إنستغرام، تيك توك، فيسبوك، تويتر...)\n"
        "أرسل لي رابط أي فيديو/صورة وسأتولى الباقي.\n\n"
        "نصائح:\n"
        "• إذا لم يعمل رابط معيّن مباشرة، جرّب رابط المنشور نفسه.\n"
        "• للحفاظ على الخطة المجانية، البوت يعمل بتنبيه/استيقاظ تلقائي عند أول رسالة.\n\n"
        "أوامر مفيدة:\n"
        "/help — كيف أستخدم البوت؟\n"
        "/about — معلومات وشروط الاستخدام."
    )
    await update.message.reply_text(text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """شرح مبسّط لطريقة الاستخدام."""
    text = (
        "طريقة الاستخدام بسيطة جدًا:\n\n"
        "1) انسخ رابط الفيديو/الصورة من التطبيق (يوتيوب/إنستا/تيك توك/فيسبوك/تويتر...)\n"
        "2) أرسِل الرابط هنا.\n"
        "3) سأحمّل الملف وأرسله لك مباشرة.\n\n"
        "⚠️ ملاحظات:\n"
        "• سأحاول دومًا اختيار صيغة جاهزة (MP4/صور) بدون أدوات خارجية.\n"
        "• بعض المواقع/الروابط قد تتطلب دمجًا لا يمكن بدون ffmpeg — في هذه الحالة قد يفشل التنزيل.\n"
        "• حقوق النشر: تحمّل مسؤولية المحتوى الذي تنزّله واستخدم الخدمة فيما هو مسموح قانونيًا."
    )
    await update.message.reply_text(text)

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معلومات وشروط الاستخدام."""
    text = (
        "حول البوت:\n"
        "• مبني بلغة Python باستخدام مكتبة python-telegram-bot و yt-dlp.\n"
        "• مصمم للعمل مجانًا على منصات تستيقظ تلقائيًا عند أول طلب (Webhooks).\n\n"
        "شروط الاستخدام:\n"
        "• الرجاء احترام حقوق النشر وسياسات المواقع.\n"
        "• لا تستخدم البوت في انتهاك القوانين أو الشروط.\n"
        "• قد تتغير سياسات المواقع، لذا قد يتوقف التنزيل لبعض الروابط مستقبلًا."
    )
    await update.message.reply_text(text)


# ===================== معالج الرسائل (الروابط) =====================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يستقبل أي نص؛ إن كان رابطًا نحاول تنزيله وإرساله.
    """
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text("أرسل رابطًا مباشرًا للفيديو/الصورة من المنصة المطلوبة 🙏")
        return

    url = match.group(0)

    # عرض حالة "كتابة..." لواجهة ألطف
    await send_action_typing(update, context)

    # مجلد تنزيل خاص بكل محادثة + جلسة (uuid) لضمان العزل والنظافة
    chat_id = update.effective_chat.id
    session_id = uuid.uuid4().hex[:8]
    download_dir = BASE_DOWNLOAD_DIR / f"{chat_id}-{session_id}"

    try:
        await update.message.reply_text("⏳ جاري التحميل... قد يستغرق الأمر ثوانٍ بحسب الرابط/الحجم.")
        loop = asyncio.get_event_loop()
        # yt-dlp بلوكي؛ ننفّذه ضمن ThreadPool حتى لا نوقف البوت كله
        files = await loop.run_in_executor(None, download_with_ytdlp, url, download_dir)

        if not files:
            await update.message.reply_text("لم أستطع تنزيل الملف من هذا الرابط. جرّب رابطًا آخر أو أعد المحاولة لاحقًا.")
            return

        # أرسل كل ملف على حدة بالطريقة الأنسب
        sent_any = False
        for fp in files:
            method = pick_send_method(fp)
            try:
                if method == "photo":
                    await update.message.reply_photo(photo=InputFile(open(fp, "rb")), caption=fp.name[:90])
                elif method == "video":
                    # ملاحظة: إذا كان الملف كبيرًا جدًا قد يرفض تيليجرام إرساله
                    await update.message.reply_video(video=InputFile(open(fp, "rb")), caption=fp.name[:90])
                else:
                    await update.message.reply_document(document=InputFile(open(fp, "rb")), caption=fp.name[:90])
                sent_any = True
            except Exception as e:
                logger.exception("فشل إرسال %s: %s", fp, e)
                # نحاول كـ Document إن فشل كصورة/فيديو
                if method != "document":
                    try:
                        await update.message.reply_document(document=InputFile(open(fp, "rb")), caption=fp.name[:90])
                        sent_any = True
                    except Exception as e2:
                        logger.exception("فشل إرسال كوثيقة أيضًا %s: %s", fp, e2)

        if sent_any:
            await update.message.reply_text("✅ تم! إذا أردت رابطًا آخر أرسله الآن.\n"
                                            "للمساعدة: /help — وللمعلومات: /about")
        else:
            await update.message.reply_text("للأسف لم أتمكن من إرسال أي ملف. ربما كان الحجم كبيرًا جدًا أو الرابط غير مدعوم.")

    except Exception as e:
        logger.exception("خطأ أثناء التنزيل: %s", e)
        await update.message.reply_text("❌ حصل خطأ أثناء التحميل. جرّب لاحقًا أو برابط مختلف.")
    finally:
        # نظف المجلد المؤقت لتقليل الاستهلاك
        cleanup_dir(download_dir)


# ===================== نقطة تشغيل التطبيق =====================

def main() -> None:
    """تهيئة البوت والعمل بـ polling محليًا و webhook على Render للحفاظ على الخطة المجانية."""
    if not BOT_TOKEN:
        raise RuntimeError("يرجى ضبط متغير البيئة BOT_TOKEN بتوكن البوت من @BotFather")

    application = Application.builder().token(BOT_TOKEN).build()

    # ربط الأوامر
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help",  cmd_help))
    application.add_handler(CommandHandler("about", cmd_about))

    # أي رسالة نصية تُمرر لمُعالج الروابط
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if APP_URL:
        # وضع Webhook: مخصص للاستضافة على Render حتى ينام/يستيقظ تلقائيًا
        # - Telegram سيضرب URL أدناه عند أول رسالة → الخدمة تستيقظ تلقائيًا
        # - url_path نستخدم فيه التوكن كمسار سري بسيط
        webhook_path = f"/{BOT_TOKEN}"
        webhook_url  = f"{APP_URL.rstrip('/')}{webhook_path}"
        print(f"[i] تشغيل بالـ webhook على: {webhook_url} (PORT={PORT})")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=webhook_url,
            # ملاحظة: run_webhook تُدير خادوم HTTP داخلي؛ لا داعي لـ gunicorn
        )
    else:
        # وضع Polling: مناسب للتجربة محليًا
        print("[i] لم يتم ضبط APP_URL — سيتم التشغيل بالـ polling محليًا.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
