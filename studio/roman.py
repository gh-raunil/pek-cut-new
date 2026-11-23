"""Hindi and Urdu in Roman letters ("Hinglish"), for captions: चूना जो उसको -> chuna jo usko, یار کیا ہے -> yaar kya hai.

Not a translation: the same word, spelled the way people type it in chats. The AI does this when it tidies a
clip's captions (brain.fix_caption_words); this is the fallback for when there is no AI, or it missed a word, so
that a caption is never drawn in Hindi or Urdu script. Urdu is written without most vowels, so common words come
from a list and the rest are spelled from their letters: readable, if not always how a person would type it."""
import re
import unicodedata

_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai",
           "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_SIGNS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o",
          "ौ": "au", "ॉ": "o", "ॅ": "e"}
_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
         "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh",
         "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v",
         "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
         # Decomposed nukta letters (base letter + nukta sign \u093C):
         "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "फ़": "f", "ड़": "d", "ढ़": "dh", "य़": "y",
         # Precomposed nukta letters (\u0958-\u095F in NFC Devanagari):
         "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "d", "ढ़": "dh", "फ़": "f", "य़": "y"}
_NUKTA = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "फ": "f", "ड": "d", "ढ": "dh", "य": "y"}
_VIRAMA, _NUKTA_SIGN = "्", "़"
_NASAL = {"ं": "n", "ँ": "n"}
_OTHER = {"ः": "h", "।": ".", "॥": ".", "ॐ": "om", **{chr(0x0966 + i): str(i) for i in range(10)}}

# Common words whose everyday chat spelling or English loanword form the rules below would not guess.
_RAW_WORDS = {
    # Pronouns, conjunctions, core particles
    "में": "mein", "मैं": "main", "हैं": "hain", "है": "hai", "नहीं": "nahi", "नही": "nahi", "हूँ": "hoon",
    "हूं": "hoon", "क्या": "kya", "क्यों": "kyun", "क्यूँ": "kyun", "यह": "yeh", "वह": "woh", "और": "aur",
    "भी": "bhi", "ही": "hi", "की": "ki", "जी": "ji", "तो": "to", "हाँ": "haan", "हां": "haan", "यहाँ": "yahan",
    "यहां": "yahan", "वहाँ": "wahan", "वहां": "wahan", "कहाँ": "kahan", "कहां": "kahan", "थी": "thi",
    "रही": "rahi", "कभी": "kabhi", "अभी": "abhi", "सभी": "sabhi", "भाई": "bhai", "मुझे": "mujhe",
    "तुम": "tum", "हम": "hum", "आप": "aap", "बहुत": "bahut", "अच्छा": "accha", "ठीक": "theek", "लिए": "liye",
    # Chat Hinglish spelling overrides
    "चाहिए": "chahiye", "चाहता": "chahta", "चाहती": "chahti", "चाहते": "chahte",
    "थोड़ा": "thoda", "थोड़ी": "thodi", "थोड़े": "thode",
    "पड़ा": "pada", "पड़ी": "padi", "पड़े": "pade", "पड़ेगा": "padega", "पड़ेगी": "padegi", "पड़ेंगे": "padenge",
    "पड़ता": "padta", "पड़ती": "padti", "पड़ते": "padte",
    "गाड़ी": "gaadi", "गाड़ियां": "gaadiyan", "गाड़ियों": "gaadiyon",
    "लड़का": "ladka", "लड़की": "ladki", "लड़के": "ladke",
    "बड़ा": "bada", "बड़ी": "badi", "बड़े": "bade",
    "छोटा": "chhota", "छोटी": "chhoti", "छोटे": "chhote",
    "छोड़": "chhod", "छोड़ो": "chhodo", "छोड़ना": "chhodna",
    "पहले": "pehle", "पहला": "pehla", "पहली": "pehli",
    "रहता": "rehta", "रहती": "rehti", "रहते": "rehte",
    "कहता": "kehta", "कहती": "kehti", "कहते": "kehte",
    "ज़्यादा": "zyada", "ज्यादा": "zyada", "ज़रूर": "zaroor", "जरूर": "zaroor",
    "ज़िंदगी": "zindagi", "जिंदगी": "zindagi",
    "ऊपर": "upar", "नीचे": "neeche", "अंदर": "andar", "बाहर": "bahar",
    "पीछे": "peeche", "सामने": "saamne",
    "दोस्त": "dost", "दोस्तों": "doston", "बंदे": "bande", "बंदा": "banda",
    "लोग": "log", "लोगों": "logon", "बातें": "baatein", "बातों": "baaton",
    "चीज": "cheez", "चीजें": "cheezein", "रास्ता": "raasta",
    "होगा": "hoga", "होगी": "hogi", "होंगे": "honge",
    "सकता": "sakta", "सकती": "sakti", "सकते": "sakte",
    "करूंगा": "karunga", "करूंगी": "karungi", "करेंगे": "karenge",
    "बताओ": "batao", "दिखाओ": "dikhao", "समझ": "samajh", "समझे": "samjhe",
    # English loanwords (Tech, UI, Video, Social)
    "स्क्रीन": "screen", "स्क्रीनशॉट": "screenshot", "बटन": "button",
    "कमेंट": "comment", "कमेंट्स": "comments", "वीडियो": "video", "वीडियोज": "videos",
    "सब्सक्राइब": "subscribe", "सब्सक्राइबर": "subscriber", "सब्सक्राइबर्स": "subscribers",
    "लाइक": "like", "लाइक्स": "likes", "शेयर": "share", "फॉलो": "follow",
    "चैनल": "channel", "लिंक": "link", "पोस्ट": "post", "अपडेट": "update",
    "नोटिफिकेशन": "notification", "पॉपअप": "popup", "मैसेज": "message",
    "चैट": "chat", "वॉइस": "voice", "ऑनलाइन": "online", "ऑफलाइन": "offline",
    "डाउनलोड": "download", "अपलोड": "upload", "अकाउंट": "account", "प्रोफाइल": "profile",
    "पासवर्ड": "password", "लॉगिन": "login", "लॉगआउट": "logout", "साइन": "sign",
    "ऐप": "app", "वेबसाइट": "website", "इंटरनेट": "internet", "सर्च": "search",
    "क्लिक": "click", "टैप": "tap", "डबल": "double", "स्क्रॉल": "scroll", "स्वाइप": "swipe",
    "फोन": "phone", "फ़ोन": "phone", "यूट्यूब": "YouTube", "कंटेंट": "content",
    "लोकेशन": "location", "एनिमेशन": "animation", "व्यू": "view",
    # English loanwords (Gaming, Action, Creators)
    "डैमेज": "damage", "एग्जिट": "exit", "फ्लाई": "fly", "ड्रिफ्ट": "drift", "ग्राउंड": "ground",
    "एनिमी": "enemy", "एनिमीज": "enemies", "बुलेट": "bullet", "बुलेट्स": "bullets",
    "डोज": "dodge", "फाइट": "fight", "गन": "gun", "गन्स": "guns", "किल": "kill", "किल्स": "kills",
    "सेव": "save", "हेल्थ": "health", "हील": "heal", "हीलिंग": "healing", "व्हीकल": "vehicle",
    "लॉबी": "lobby", "गेमिंग": "gaming", "गेमप्ले": "gameplay", "प्लेयर": "player", "प्लेयर्स": "players",
    "ट्रिक": "trick", "ट्रिक्स": "tricks", "हैक": "hack", "हैक्स": "hacks", "फायर": "fire", "शूट": "shoot",
    "हेडशॉट": "headshot", "बॉडीशॉट": "bodyshot", "नॉक": "knock", "रिवाइव": "revive", "रीलोड": "reload",
    "स्कोप": "scope", "एम": "aim", "ड्रॉप": "drop", "एयरड्रॉप": "airdrop", "लूट": "loot",
    "जोन": "zone", "सेफ": "safe", "रश": "rush", "कैंप": "camp", "कैंपर": "camper", "रैंक": "rank",
    "पुश": "push", "टियर": "tier", "मैच": "match", "राउंड": "round", "टीम": "team", "स्क्वाड": "squad",
    "सोलो": "solo", "डुओ": "duo", "सर्वर": "server", "पिंग": "ping", "लैग": "lag", "ग्लिच": "glitch",
    "बग": "bug", "स्पॉन": "spawn", "टेलिपोर्ट": "teleport", "टेलीपोर्ट": "teleport",
    "सुमन": "summon", "समन": "summon", "पेगासस": "pegasus", "हॉर्स": "horse",
    "पावर": "power", "सुपर": "super", "अनलॉक": "unlock", "लेवल": "level", "मिशन": "mission",
    "इवेंट": "event", "रिवॉर्ड": "reward", "स्किन": "skin", "इमोट": "emote", "कस्टम": "custom",
    "मिथ्स": "myths", "मिथ": "myth", "केबिन": "cabin", "टावर": "tower", "फेदर": "feather",
    "बूस्ट": "boost", "क्लासिक": "classic", "चेस्ट": "chest", "स्टोन": "stone", "ब्लिंक": "blink",
    "हाइड": "hide", "हाइंड": "hide", "पंच": "punch", "मिली": "melee", "वेपन": "weapon",
    "पैसिव": "passive", "स्किल": "skill", "स्किल्स": "skills", "सील": "seal", "एनर्जी": "energy",
    "विंटर": "winter", "प्रोन": "prone", "स्लाइड": "slide", "हॉर्न": "horn", "मेडिकिट": "medkit",
    "पावनिन": "Pownin", "अरोरा": "Aurora", "विकेंडी": "Vikendi", "इरेंगल": "Erangel", "लिविक": "Livik",
    # Everyday loanwords
    "लेफ्ट": "left", "राइट": "right", "अप": "up", "डाउन": "down",
    "स्टार्ट": "start", "स्टॉप": "stop", "पॉज": "pause", "प्ले": "play", "नेक्स्ट": "next",
    "बैक": "back", "फ्रंट": "front", "साइड": "side", "टॉप": "top", "बॉटम": "bottom", "सेंटर": "center",
    "फुल": "full", "हाफ": "half", "टाइम": "time", "टाइम्स": "times", "सेकंड": "second", "सेकंड्स": "seconds",
    "मिनट": "minute", "मिनट्स": "minutes", "घंटे": "hours", "फास्ट": "fast", "स्लो": "slow",
    "इजी": "easy", "हार्ड": "hard", "सिंपल": "simple", "नॉर्मल": "normal", "परफेक्ट": "perfect",
    "बेस्ट": "best", "गुड": "good", "बैड": "bad", "कूल": "cool", "नाइस": "nice", "अमेजिंग": "amazing",
    "क्रेजी": "crazy", "लिटरली": "literally", "रियली": "really", "एक्चुअली": "actually",
    "डेफिनेटली": "definitely", "बेसिकली": "basically", "फाइनली": "finally", "ऑब्वियसली": "obviously",
    "प्रॉब्लम": "problem", "सॉल्यूशन": "solution", "आइडिया": "idea", "प्लान": "plan", "ऑप्शन": "option",
    "सेटिंग": "setting", "सेटिंग्स": "settings", "फीचर": "feature", "फीचर्स": "features",
    "सीक्रेट": "secret", "सीक्रेट्स": "secrets", "टिप्स": "tips", "टिप": "tip", "चेक": "check",
    "टेस्ट": "test", "ट्राई": "try", "यूज": "use", "यूज़": "use", "युज": "use", "युज़": "use",
    "हेल्प": "help", "सपोर्ट": "support", "वेट": "wait", "रेडी": "ready", "फिनिश": "finish",
    "कम्प्लीट": "complete", "क्लियर": "clear", "डाउट": "doubt", "श्योर": "sure", "प्लीज": "please",
    "थैंक्स": "thanks", "थैंक": "thank", "वेलकम": "welcome", "सॉरी": "sorry", "ओके": "okay",
    "हे": "hey", "हेलो": "hello", "हाय": "hi", "बाय": "bye", "गाइज": "guys", "फ्रेंड्स": "friends",
    "ब्रो": "bro", "सर": "sir", "बॉस": "boss", "पार्ट": "part", "नंबर": "number",
    "फर्स्ट": "first", "लास्ट": "last", "न्यू": "new", "ओल्ड": "old", "ओपन": "open", "क्लोज": "close",
    "हाउस": "house", "अपार्टमेंट": "apartment", "बिल्डिंग": "building", "रूम": "room",
    "डोर": "door", "विंडो": "window", "वॉल": "wall", "रूफ": "roof", "एंट्रेंस": "entrance",
    "स्टक": "stuck", "बैग": "bag", "कार": "car", "बाइक": "bike", "रोड": "road", "ब्रिज": "bridge",
    "वाटर": "water", "कैमरा": "camera", "माइक": "mic", "साउंड": "sound", "म्यूजिक": "music",
    "ऑडियो": "audio", "कलर": "color", "साइज": "size", "मनी": "money", "कैश": "cash",
    "प्राइस": "price", "कॉस्ट": "cost", "फ्री": "free", "पे": "pe", "शॉप": "shop",
    "मार्केट": "market", "अमेजॉन": "amazon", "गिफ्ट": "gift", "बॉक्स": "box", "पैक": "pack",
    "इंस्टेंट": "instant", "ऐड": "add", "सेलेक्ट": "select", "एंड": "and", "ऑलरेडी": "already",
    "जस्ट": "just", "बट": "but", "फन": "fun", "फूड": "food", "अदरवाइज": "otherwise", "इंफो": "info",
    "इज": "is", "ऑफ": "of", "रीजन": "reason", "वाले": "wale", "वाला": "wala", "वाली": "wali",
    "कुछ": "kuch", "सेम": "same", "देन": "then", "इसमें": "ismein", "वो": "woh", "वापस": "wapas",
    "रैंडम": "random", "ऑल": "all", "इवन": "even", "डायरेक्ट": "direct", "थ्री": "three",
    "नॉस्टैल्जिया": "nostalgia", "नोस्टेल्जिया": "nostalgia", "अराइविंग": "arriving", "हियर": "here",
    "विद": "with", "नाउ": "now", "हायर": "hire", "गिव": "give", "मी": "me", "आइटम": "item",
    "कैन्ड": "canned", "मल्टीपल": "multiple", "कम्प्लीटली": "completely", "टेक": "take",
    "केयर": "care", "बाय-बाय": "bye-bye", "टाटा": "tata",
}
# Normalize both NFC and NFD keys so lookup never fails due to Unicode normalization forms.
_WORDS = {}
for _k, _v in _RAW_WORDS.items():
    _WORDS[unicodedata.normalize("NFC", _k)] = _v
    _WORDS[unicodedata.normalize("NFD", _k)] = _v
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
OTHER_SCRIPT = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")   # Urdu / Arabic letters


def has_hindi(text: str) -> bool:
    return bool(DEVANAGARI.search(text or ""))


def needs_roman(text: str) -> bool:
    """Is this in a script that captions should show in Roman letters instead (Hindi, Urdu, Arabic, Russian-style,
    Greek or Korean letters)?"""
    text = text or ""
    return bool(DEVANAGARI.search(text) or OTHER_SCRIPT.search(text) or MORE_SCRIPTS.search(text))


def drawable(text: str) -> bool:
    """Can the caption fonts draw this? They have Latin letters (with accents), Devanagari, digits and punctuation."""
    return not any(ch.isalpha() and not (ord(ch) < 0x250 or 0x1E00 <= ord(ch) <= 0x1EFF or 0x0900 <= ord(ch) <= 0x097F) for ch in text or "")


# ---------------------------------------------------------------- Russian-style (Cyrillic), Greek and Korean letters
MORE_SCRIPTS = re.compile(r"[\u0370-\u03FF\u0400-\u04FF\uAC00-\uD7A3]")
_CYR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y",
        "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
        "я": "ya", "є": "ye", "і": "i", "ї": "yi", "ґ": "g", "ў": "u", "ђ": "dj", "ј": "j", "љ": "lj", "њ": "nj",
        "ћ": "c", "џ": "dz", "ә": "a", "ғ": "g", "қ": "q", "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h"}
_GRK = {"α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th", "ι": "i", "κ": "k", "λ": "l",
        "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "φ": "f",
        "χ": "ch", "ψ": "ps", "ω": "o"}
_K_FIRST = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_K_VOWEL = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu",
            "eu", "ui", "i"]
_K_LAST = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p", "t", "t", "ng",
           "t", "t", "k", "t", "p", "t"]


def _letters(run: str) -> str:
    import unicodedata
    out = []
    for ch in unicodedata.normalize("NFD", run):
        if unicodedata.combining(ch):
            continue                                   # Greek accents
        low = ch.lower()
        r = _CYR.get(low, _GRK.get(low))
        if r is None:
            out.append(ch)
        else:
            out.append(r.capitalize() if ch != low and r else r)
    return "".join(out)


def _korean(run: str) -> str:
    out = []
    for ch in run:
        n = ord(ch) - 0xAC00
        out.append(_K_FIRST[n // 588] + _K_VOWEL[n % 588 // 28] + _K_LAST[n % 28] if 0 <= n < 11172 else ch)
    return "".join(out)


def _word(w: str) -> str:
    w = unicodedata.normalize("NFC", w)
    prefix, suffix = "", ""
    while w and w[0] in ("।", "॥", ".", ",", "!", "?", "-", ":", ";"):
        prefix += _OTHER.get(w[0], w[0])
        w = w[1:]
    while w and w[-1] in ("।", "॥", ".", ",", "!", "?", "-", ":", ";"):
        suffix = _OTHER.get(w[-1], w[-1]) + suffix
        w = w[:-1]
    if not w:
        return prefix + suffix
    if w in _WORDS:
        return prefix + _WORDS[w] + suffix
    # One unit per letter: [roman consonant or vowel, its vowel or None (None = a consonant with no vowel), is_cons]
    units, i = [], 0
    while i < len(w):
        ch = w[i]
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if ch in _CONS:
            c = _CONS[ch]
            if nxt == _NUKTA_SIGN:
                c = _NUKTA.get(ch, c)
                i += 1
                nxt = w[i + 1] if i + 1 < len(w) else ""
            if nxt == _VIRAMA:
                units.append([c, None, True, False])
                i += 2
                continue
            if nxt in _SIGNS:
                units.append([c, _SIGNS[nxt], True, False])
                i += 2
                continue
            units.append([c, "a", True, True])        # the vowel every Hindi letter carries unless told otherwise
        elif ch in _VOWELS:
            units.append(["", _VOWELS[ch], False, False])
        elif ch in _NASAL:
            if units:
                units[-1][1] = (units[-1][1] or "") + "n"
                units[-1][3] = False
                units[-1].append("nasal")
            else:
                units.append(["n", None, True, False])
        elif ch in _OTHER:
            units.append([_OTHER[ch], None, False, False])
        elif ch == _NUKTA_SIGN or ch == _VIRAMA:
            pass
        else:
            units.append([ch, None, False, False])
        i += 1
    letters = [u for u in units if u[2] or u[1]]
    # Hindi drops the carried "a" at the end of a word, and between a vowel and another vowel-carrying letter
    # (कहना -> kahna, not kahana). Looked at right to left, as speakers do.
    if len(letters) > 1 and letters[-1][3]:
        letters[-1][1] = None
    for k in range(len(letters) - 2, 0, -1):
        u, before, after = letters[k], letters[k - 1], letters[k + 1]
        if u[3] and before[1] and after[2] and after[1] and "nasal" not in before:
            u[1] = None
    # Long vowels the way people type them: doubled only before a consonant that ends a syllable (baat, kaam,
    # aaj), single elsewhere (hamara, pata, ki, tu).
    short = {"aa": "a", "ee": "i", "oo": "u"}
    for k, u in enumerate(letters):
        if u[1] in short:
            after = letters[k + 1] if k + 1 < len(letters) else None
            if after is None or after[1]:
                u[1] = short[u[1]]
    return prefix + "".join(u[0] + (u[1] or "") for u in units) + suffix


# ---------------------------------------------------------------- Urdu
_UR_WORDS = {"ہے": "hai", "ہیں": "hain", "ہوں": "hoon", "ہو": "ho", "ہی": "hi", "یہ": "ye", "وہ": "wo", "کیا": "kya",
             "کیوں": "kyun", "نہیں": "nahi", "نہ": "na", "میں": "main", "میرا": "mera", "میری": "meri", "میرے": "mere",
             "تو": "to", "کہ": "ke", "کی": "ki", "کا": "ka", "کے": "ke", "کو": "ko", "سے": "se", "پر": "par",
             "اور": "aur", "بھی": "bhi", "تھا": "tha", "تھی": "thi", "تھے": "the", "گیا": "gaya", "گئی": "gayi",
             "رہا": "raha", "رہی": "rahi", "رہے": "rahe", "بہت": "bahut", "اچھا": "achha", "یار": "yaar", "بھائی": "bhai",
             "آپ": "aap", "ہم": "hum", "تم": "tum", "مجھے": "mujhe", "تمہیں": "tumhein", "اب": "ab", "کر": "kar",
             "کرو": "karo", "کرنا": "karna", "دیکھو": "dekho", "چلو": "chalo", "ابھی": "abhi", "کبھی": "kabhi",
             "جی": "ji", "ہاں": "haan", "نا": "na", "اللہ": "Allah", "ٹھیک": "theek", "پتا": "pata", "پتہ": "pata",
             "لیکن": "lekin", "پھر": "phir", "سب": "sab", "کچھ": "kuch", "کون": "kaun", "کہاں": "kahan", "یہاں": "yahan",
             "وہاں": "wahan", "ایک": "ek", "دو": "do", "اپنا": "apna", "اپنی": "apni", "لوگ": "log", "بات": "baat",
             "کام": "kaam", "دن": "din", "آج": "aaj", "کل": "kal", "بس": "bas", "شکریہ": "shukriya", "واہ": "wah",
             "گھر": "ghar", "ہوا": "hua", "ہوئی": "hui", "ہوئے": "hue", "اس": "us", "ان": "un", "جو": "jo", "جب": "jab",
             "تب": "tab", "مگر": "magar", "پاکستان": "Pakistan", "زندگی": "zindagi", "پیسے": "paise", "روپے": "rupay"}
_UR_CONS = {"ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "ch", "ح": "h", "خ": "kh", "د": "d",
            "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s", "ض": "z",
            "ط": "t", "ظ": "z", "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "ك": "k", "گ": "g", "ل": "l", "م": "m",
            "ن": "n", "ہ": "h", "ه": "h", "ة": "a", "ھ": "h"}
_UR_MARKS = {"َ": "a", "ِ": "i", "ُ": "u", "ّ": "", "ْ": "", "ً": "an", "ٰ": "a"}
_UR_DIGITS = {**{chr(0x06F0 + i): str(i) for i in range(10)}, **{chr(0x0660 + i): str(i) for i in range(10)}}
_AR_PUNCT = {"،": ",", "؟": "?", "؛": ";", "٪": "%"}    # Arabic commas and question marks: the caption fonts lack them


def _urdu_word(w: str) -> str:
    if w in _UR_WORDS:
        return _UR_WORDS[w]
    out, prev_cons = [], False
    for i, ch in enumerate(w):
        first, last = i == 0, i == len(w) - 1
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if ch in _UR_MARKS:
            out.append(_UR_MARKS[ch])
            prev_cons = False
            continue
        if ch in _UR_DIGITS:
            out.append(_UR_DIGITS[ch])
            prev_cons = False
            continue
        if ch == "ھ":                     # do chashmi he: the "h" of bh, ph, kh, chh...
            out.append("h")
            continue
        if ch in _AR_PUNCT:
            out.append(_AR_PUNCT[ch])
            prev_cons = False
            continue
        if ch in "اآأإٱ":                 # alef, and alef with hamza or wasla (أنا, إن): Arabic writes these often
            out.append("aa" if ch == "آ" else "i" if ch == "إ" else "a")
            prev_cons = False
            continue
        if ch in "وؤ":
            if first or not prev_cons:
                out.append("w" if nxt not in "اآ" else "w")
                prev_cons = True
            else:
                out.append("o")
                prev_cons = False
            continue
        if ch in "یيئى":
            if first:
                out.append("y")
                prev_cons = True
            else:
                out.append("i" if last else "ee" if prev_cons and nxt not in "اآوؤ" else "y")
                prev_cons = out[-1] == "y"
            continue
        if ch == "ے":
            out.append("e")
            prev_cons = False
            continue
        if ch in "ںن" and ch == "ں":
            out.append("n")
            continue
        if ch in "عء":
            continue
        c = _UR_CONS.get(ch)
        if c is None:
            out.append(ch)
            prev_cons = False
            continue
        if ch in "ہه" and last and prev_cons:
            out.append("a")               # a final ہ after a consonant is a vowel: کہ, بچہ -> bacha
            prev_cons = False
            continue
        if prev_cons and i == 1 or (prev_cons and i == 2 and w[1] == "ھ"):
            out.append("a")               # the short "a" Urdu does not write after a word's first letter: گھر -> ghar
        out.append(c)
        prev_cons = True
    s = "".join(out)
    if re.fullmatch(r"[a-z]{2,}", s) and not re.search(r"[aeiou]", s):
        s = s[:-1] + "a" + s[-1]          # no vowel at all: give it the one Urdu leaves out
    return s


_HINDI_RUN = r"[ऀ-ॿ]+"
_URDU_RUN = r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+"


def to_roman(text: str) -> str:
    """Every Hindi and Urdu word in the text spelled in Roman letters. Anything else is left exactly as it is."""
    text = text or ""
    if DEVANAGARI.search(text):
        text = re.sub(_HINDI_RUN, lambda m: _word(m.group(0)), text)
    if OTHER_SCRIPT.search(text):
        text = text.replace("،", ",").replace("؟", "?").replace("۔", ".")
        text = re.sub(_URDU_RUN, lambda m: _urdu_word(m.group(0)), text)
    if MORE_SCRIPTS.search(text):
        import unicodedata
        text = "".join(unicodedata.normalize("NFD", ch) if "\u0370" <= ch <= "\u04FF" or "\u1F00" <= ch <= "\u1FFF" else ch for ch in text)
        text = re.sub(r"[\uAC00-\uD7A3]+", lambda m: _korean(m.group(0)), text)
        text = unicodedata_nfc(re.sub(r"[\u0370-\u03FF\u0400-\u04FF\u0300-\u036F]+", lambda m: _letters(m.group(0)), text))
    return text


def unicodedata_nfc(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", s)


def words(ws: list) -> list:
    """Caption words ({'w', 's', 'e'}) with any Hindi or Urdu in Roman letters. Timings are untouched."""
    return [dict(w, w=to_roman(w["w"])) if needs_roman(w.get("w", "")) else w for w in ws]
