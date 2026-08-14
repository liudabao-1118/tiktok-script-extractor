#!/usr/bin/env python3
"""
Video type & structure analyzer (rule-based, no LLM required).

Classifies each TikTok video into:
  - video_type:     达人视频 | 自制视频 | 品牌广告 | 未知
  - video_structure: 达人口播 | AI生成 | 混剪 | 图文 | 剧情 | 测评 | 未知

Heuristics use the video TITLE/description, the transcript (original_text),
and the author handle. This is a lightweight, dependency-free classifier.

Upgrade path: if an LLM key is available, replace `_classify_*` with an
LLM prompt for much higher accuracy. See translator.py for the LLM hook.
"""


class VideoAnalyzer:
    def analyze(self, description="", original_text="", author=""):
        vtype = self._classify_type(description, original_text, author)
        structure = self._classify_structure(description, original_text)
        return vtype, structure

    # ---------- video type ----------
    def _classify_type(self, description, original_text, author):
        text = (description + " " + original_text).lower()
        author_l = (author or "").lower()

        # 1) paid promotion markers -> 品牌广告
        promo = ["#广告", "#推广", "#赞助", "#合作", "#试用", "#ad",
                 "sponsored", "广告", "推广", "商务合作", "品牌合作"]
        if any(k in text for k in promo):
            return "品牌广告"

        # 2) brand official account -> 自制视频
        brand = ["official", "官方", "旗舰店", "官网", "总部", "studio"]
        if any(k in author_l for k in brand) or any(k in (description or "").lower() for k in brand):
            return "自制视频"

        # 3) default: individual creator -> 达人视频
        return "达人视频"

    # ---------- video structure ----------
    def _classify_structure(self, description, original_text):
        d = (description or "").lower()
        t = original_text or ""
        tl = t.lower()
        combo = d + " " + tl

        # 1) AI-generated
        ai_markers = ["ai生成", "人工智能生成", "数字人", "虚拟人", "midjourney",
                      "sora", "ai配音", "ai 配音", "用ai", "由ai", "ai工具",
                      "chatgpt", "stable diffusion", "即梦", "可灵"]
        if any(k in combo for k in ai_markers):
            return "AI生成"

        # 2) 混剪 (music video / lyrics have a transcript but it's song/fragment)
        if "♪" in t or "歌词" in t:
            return "混剪"

        # 3) 测评 / 开箱 / review
        review = ["测评", "评测", "开箱", "试用了一", "值得买", "优缺点",
                  "上手", "体验了", "真实测评"]
        if any(k in t for k in review):
            return "测评"

        # 4) 剧情 / 短剧 (dialogue, acting)
        story = ["剧情", "扮演", "演员", "场景", "旁白", "对白", "台词",
                 "短剧", "演技", "角色"]
        if any(k in t for k in story):
            return "剧情"

        # 5) very short fragmented caption -> 混剪
        if len(t.strip()) < 25:
            return "混剪"

        # 6) default: talking-head monologue -> 达人口播
        return "达人口播"
