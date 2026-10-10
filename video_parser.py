"""
视频解析插件 - 检测群内分享的视频链接，解析并发送无水印版本
支持平台：B站、抖音、微博、小红书、快手、知乎、微信视频号、acfun、
YouTube、TikTok、Instagram、Twitter、Iwara、Pixiv、网易云、QQ空间

解析核心：astrbot_plugin_parser 复刻版（video_parser_core）
来源仓库：https://github.com/Zhalslar/astrbot_plugin_parser
本文件为适配 NapCat-WordLibBot 的魔改版（去旧版解析逻辑，仅保留新核心），
video_parser_core/ 目录为该仓库核心代码的移植（含 astrbot 兼容层）。
"""

# ========== 插件元数据（SDK 规范）==========
__plugin_name_cn__ = "视频解析"
__plugin_name_en__ = "video_parser"
__plugin_version__ = "1.0.0"
__plugin_desc__ = "多平台视频/图文解析去水印（B站/抖音/微博/小红书/快手/YouTube等16平台）"
__plugin_author__ = "NapCat-WordLibBot"

# ============================================================================
# 可配置项声明（面板据此**自动生成表单**，见 web/config_schema.py）
#   · 字段名 → 控件类型；path 是值在配置文件里的位置（settings.foo / commands.bar）
#   · label/help/group 是面板上直接显示的中文；advanced 折叠进「高级选项」
#   · 纯元数据，不改变插件任何行为；未声明的插件面板会按当前值自动推断
# ============================================================================

__config_schema__ = {
    # ---------------- 基础参数（顶层平铺）----------------
    "enabled": {
        "type": "bool", "label": "插件总开关", "group": "参数",
        "path": "enabled",
        "help": "全局总开关，关闭后所有群都不再解析（分群开关见「群组管理」）",
        "default": True,
    },
    "show_download_fail_tip": {
        "type": "bool", "label": "下载失败时提示", "group": "参数",
        "path": "show_download_fail_tip",
        "help": "解析成功但下载失败时，是否在群里提示一句",
        "default": True,
    },
    "audio_to_file": {
        "type": "bool", "label": "音频以文件发送", "group": "参数",
        "path": "audio_to_file",
        "help": "关闭时音频按语音发送，开启时按文件发送（避免语音被压缩）",
    },
    "debounce_seconds": {
        "type": "number", "label": "解析防抖间隔", "group": "参数",
        "path": "debounce_seconds", "min": 0, "max": 600, "step": 1, "unit": "秒",
        "help": "同一会话里相同链接/资源在该时间内不重复解析，0=关闭防抖",
        "default": 30,
    },

    # ---------------- 表情回应（解析命中后回一个表情）----------------
    "react_on_start": {
        "type": "bool", "label": "命中链接时回表情", "group": "表情回应",
        "path": "react_on_start",
        "help": "解析核心确认命中受支持链接后，给触发消息回一个表情（告知正在解析）",
        "default": True,
    },
    "react_emoji_ids": {
        "type": "list", "item_type": "number", "label": "表情池", "group": "表情回应",
        "path": "react_emoji_ids",
        "help": "每次随机抽一个的 QQ 表情 ID 列表（如 128064=👀、128269=🔍、129300=🤔）；留空回退旧单值键",
        "default": [128064, 128269, 129300, 128170, 128077, 128079, 128640, 10024],
    },
    "react_emoji_id": {
        "type": "number", "label": "表情 ID（旧版单值）", "group": "表情回应", "advanced": True,
        "path": "react_emoji_id", "min": 0, "max": 999999, "step": 1,
        "help": "旧版单值配置，仅在表情池为空时作为兜底；当前配置里存在此键但已不再优先使用",
        "default": 128064,
    },
    "react_remove_if_no_link": {
        "type": "bool", "label": "非链接时撤销表情（未生效）", "group": "表情回应", "advanced": True,
        "path": "react_remove_if_no_link",
        "help": "历史遗留键：现在只有真正命中解析才回表情，不会误标，此键不再被读取",
        "default": True,
    },

    # ---------------- 解析核心（16 平台）----------------
    "forward_threshold": {
        "type": "number", "label": "合并转发阈值", "group": "解析核心", "advanced": True,
        "path": "forward_threshold", "min": 1, "max": 50, "step": 1, "unit": "段",
        "help": "一条消息要发的内容段数达到该值就改用合并转发",
        "default": 4,
    },
    "proxy": {
        "type": "text", "label": "代理地址", "group": "解析核心", "advanced": True,
        "path": "proxy",
        "help": "给下载器/解析器用的代理，如 http://127.0.0.1:7890；留空不使用（YouTube 等需要）",
    },
    "browser_timeout": {
        "type": "number", "label": "下载超时", "group": "解析核心", "advanced": True,
        "path": "browser_timeout", "min": 5, "max": 600, "step": 5, "unit": "秒",
        "help": "单个资源下载的超时时间（若另配了 download_timeout，后者优先）",
        "default": 60,
    },
    "download_retry_times": {
        "type": "number", "label": "下载重试次数", "group": "解析核心", "advanced": True,
        "path": "download_retry_times", "min": 0, "max": 10, "step": 1, "unit": "次",
        "default": 3,
    },
    "download_max_size": {
        "type": "number", "label": "下载大小上限", "group": "解析核心", "advanced": True,
        "path": "download_max_size", "min": 1, "max": 4096, "step": 1, "unit": "MB",
        "help": "超过该大小的媒体不下载（防止群里被大文件拖死）",
        "default": 1024,
    },
    "download_max_duration": {
        "type": "number", "label": "视频时长上限", "group": "解析核心", "advanced": True,
        "path": "download_max_duration", "min": 10, "max": 604800, "step": 10, "unit": "秒",
        "help": "超过该时长的视频不下载（上限放到 7 天，因为依星实例当前值是 360000 秒=100 小时）",
        "default": 360000,
    },
    "bili_video_quality": {
        "type": "select", "label": "B站画质", "group": "解析核心", "advanced": True,
        "path": "bili_video_quality",
        "options": ["P_360P", "P_480P", "P_720P", "P_1080P"],
        "help": "越高越清晰、下载越慢；部分高画质需要大会员 Cookie",
        "default": "P_720P",
    },
    "bili_video_codec": {
        "type": "select", "label": "B站编码", "group": "解析核心", "advanced": True,
        "path": "bili_video_codec",
        "options": ["AVC", "HEVC", "AV1"],
        "help": "AVC 兼容性最好（QQ 播放稳妥）",
        "default": "AVC",
    },
    "douyin_cookie": {
        "type": "password", "label": "抖音 Cookie", "group": "凭证", "advanced": True,
        "path": "douyin_cookie",
        "help": "从浏览器复制的抖音 Cookie，用于去水印/高码率；失效后需重新复制",
        "default": "_tea_utm_cache_1243=undefined; MONITOR_WEB_ID=6e6c0af9-d141-40bc-a265-627e1c7f4c06; dy_swidth=443; dy_sheight=959; SEARCH_RESULT_LIST_TYPE=%22single%22; x-web-secsdk-uid=4b514d37-c353-4ff4-8c10-9ae54c182a37; csrf_session_id=6e4779d201e1b35732dc8bdb6b94f316; home_can_add_dy_2_desktop=%220%22; hevc_supported=true; SEARCH_UN_LOGIN_PV_CURR_DAY=%7B%22date%22%3A1780091369803%2C%22count%22%3A1%7D; ttwid=1%7CdEqq7xTIJcyxDGUg8qLO0yGiDPbum6jC-e29Jv39Zns%7C1780091373%7Cd8dfcab00c615f510f0e3b6e13e99f205911dc48e407d96d9eea860d3fdfc994; s_v_web_id=verify_mprgfybx_Ha30hhFa_wklu_4ihC_BsOt_VqeHSEolZ9zn; fpk1=U2FsdGVkX1/RroyiZcpYPMF4sukIYlCS+4oe0pk2FGhyYP5E5BILUcHarhJg4sPHeWOj5drd0/6A+MobN0hKGw==; fpk2=3357fadb0316939352bbdd4d5360a97f; passport_csrf_token=1ac19982a54cfc36872659e8ec5dd8a6; passport_csrf_token_default=1ac19982a54cfc36872659e8ec5dd8a6; bd_ticket_guard_client_web_domain=2; sdk_source_info=7e276470716a68645a606960273f276364697660272927676c715a6d6069756077273f276364697660272927666d776a68605a607d71606b766c6a6b5a7666776c7571273f275e58272927666a6b766a69605a696c6061273f27636469766027292762696a6764695a7364776c6467696076273f275e582729277672715a646971273f2763646976602729277f6b5a666475273f2763646976602729276d6a6e5a6b6a716c273f27717770602729276c6b6f5a7f6367273f27636469766027292771273f27323d32313236343c35353d3234272927676c715a75776a716a666a69273f2763646976602778; bit_env=jkOVjd9fBCUAdUZy1I1xRwlCoMVCH3Yq9cRJujs02G7KYWMejGRWPI_dQs2jwaBvR0jPW9iPOxJwJTO0BJc__Fm4Zo6EqMGrPWGOQRlIabHCfbnotKGTOkpZj_aCkpUj6ZxLbo1Th0MzJIv8rc4reYftslKAXeqGKtVDiokOEYC_KRyvcOnE4sZeUK_TTpGCPuvYKD-x0d7LSaWXMK-VrqDtLuQlcAa5XHnYGYeSY4om4Xj4idxsF5CcaI2bUzwYOhzvtTxZyZW6Z1IXbTXlJs6KxSuirXsIRCjB796pJQ-qZZp9WfwueJi_qiPz1wtoKqRw8ERow8qcVniN576vnJkuMqw97LgyN02yX3suiycNGxp85S0C_5f6myodL8Hlxi-6qJIOVQ-mx0o2NTO2uHYqIwA146gIdHTfXQUfCyJDgKgMWNE9Py8Bj_xLXB3PMEDirWRhoyvk8h-b_4BRWLE4kJOzKtCa2t8KTeJfFt8KEjptEnPeQHK22UccOz8QJLWaQttSi6sEGR40WVY57ntp1aX49569KeqeBcmxz50%3D; gulu_source_res=eyJwX2luIjoiYzgyNzM1ZTBmODNmNjk2MTg5M2Y2OWE4ZjRjY2NhZmE4MWNmYTEwM2Y5MWJmMGRkMzExNjYwN2YzMzNmNDkyNSJ9; passport_auth_mix_state=pasx5noppq8qujr5aslgfh20cdtrt4pv; passport_mfa_token=CjYrwtz%2FGaOMACKEMh0tJc5mwRL1AGBd3mwHeXHXSHsbwHjJPK6HLA5SRe7otMORfFiuAGrw2akaSgo8AAAAAAAAAAAAAFB6oW6yM8DoRIfHPMKMAJqQQuNm8CddWw%2Bh7OxGWeexrMQN18gdIZNqBdXVT%2BeT9UiJEPvlkg4Y9rHRbCACIgEDFY6CmA%3D%3D; d_ticket=8637dc0cb7902e30efc4775e76371c4a658ff; passport_assist_user=CkAVmx5PthBWCvWTVGnkJJ7pN9SPrV4c7O_x_0p6AzZcTy9BCL7KO3MA7F80F2cDY4Wk3kvuAUL2Mcqd-0aa3BcnGkoKPAAAAAAAAAAAAABQeu2diHIwm2Nt-Azd3YwDixvq68Hvc9-IHjsmmmHJ-oFg-T4S4Sm2oOWs4XI4nDgdiRC05JIOGImv1lQgASIBA7jtmJo%3D; n_mh=4yT_8HEUerjt7IzGrg130I6zKNNwhjrWmCCqoovx1mA; passport_auth_status=6192b332dec5c8f50817f0379fef34fb%2C; passport_auth_status_ss=6192b332dec5c8f50817f0379fef34fb%2C; sid_guard=969fa5b32d4a71a44c0bc9fed76e8cdb%7C1780091492%7C5183999%7CTue%2C+28-Jul-2026+21%3A51%3A31+GMT; uid_tt=127e193bc55cfd0877b3add7ebe8f9a2; uid_tt_ss=127e193bc55cfd0877b3add7ebe8f9a2; sid_tt=969fa5b32d4a71a44c0bc9fed76e8cdb; sessionid=969fa5b32d4a71a44c0bc9fed76e8cdb; sessionid_ss=969fa5b32d4a71a44c0bc9fed76e8cdb; session_tlb_tag=sttt%7C5%7Clp-lsy1KcaRMC8n-126M2_________-2eujw1bAcvRARvO-B1ZrprHQgMQoSicZRF5jC_obgSLc%3D; is_staff_user=false; has_biz_token=false; sid_ucp_v1=1.0.0-KDA4YmJjN2RkMDMyOWQ0N2QyM2ExNmU2NDM0YTZjOTkxZjAwYWJiNzgKIAiovrC6zPRfEOSU6NAGGO8xIAwwg-ux-gU4AkDxB0gEGgJscSIgOTY5ZmE1YjMyZDRhNzFhNDRjMGJjOWZlZDc2ZThjZGI; ssid_ucp_v1=1.0.0-KDA4YmJjN2RkMDMyOWQ0N2QyM2ExNmU2NDM0YTZjOTkxZjAwYWJiNzgKIAiovrC6zPRfEOSU6NAGGO8xIAwwg-ux-gU4AkDxB0gEGgJscSIgOTY5ZmE1YjMyZDRhNzFhNDRjMGJjOWZlZDc2ZThjZGI; _bd_ticket_crypt_cookie=a0d837e99d7362a2b7b67c0844904254; __security_mc_1_s_sdk_sign_data_key_web_protect=c283c862-4cfe-947f; __security_mc_1_s_sdk_cert_key=9747310c-46c1-969b; __security_mc_1_s_sdk_crypt_sdk=2c340bf7-48e1-8825; __security_server_data_status=1; login_time=1780091491924; __ac_nonce=06a1a0a6400bcef3f83f5; __ac_signature=_02B4Z6wo00f01zXE6QQAAIDDMnWprUjMgXs19O2AAKd165; SelfTabRedDotControl=%5B%7B%22id%22%3A%227409907739046742027%22%2C%22u%22%3A434%2C%22c%22%3A0%7D%2C%7B%22id%22%3A%227542967602167941139%22%2C%22u%22%3A46%2C%22c%22%3A0%7D%2C%7B%22id%22%3A%227569901463195879466%22%2C%22u%22%3A106%2C%22c%22%3A0%7D%2C%7B%22id%22%3A%227565506634026321963%22%2C%22u%22%3A45%2C%22c%22%3A0%7D%5D; IsDouyinActive=true; FOLLOW_NUMBER_YELLOW_POINT_INFO=%22MS4wLjABAAAAvyUxUHwpxRSLOhvw_icuPiuFkStQ9dZXhQO3bL0X4Pg%2F1780156800000%2F0%2F1780091497339%2F0%22; bd_ticket_guard_client_data=eyJiZC10aWNrZXQtZ3VhcmQtdmVyc2lvbiI6MiwiYmQtdGlja2V0LWd1YXJkLWl0ZXJhdGlvbi12ZXJzaW9uIjoxLCJiZC10aWNrZXQtZ3VhcmQtcmVlLXB1YmxpYy1rZXkiOiJCRGFEZnhKOWp4NXVkUWpucW1tZXV0OU1FS0lYMitHQ2dFUjdzZUxBTjFOYnJnZGtIaG51aEJpWW9FOHNkUVVCaDhIN0VnUjNqYUJGYlJnMWRmZmliajg9IiwiYmQtdGlja2V0LWd1YXJkLXdlYi12ZXJzaW9uIjoyfQ%3D%3D; biz_trace_id=8e41b051; bd_ticket_guard_client_data_v2=eyJyZWVfcHVibGljX2tleSI6IkJEYURmeEo5ang1dWRRam5xbW1ldXQ5TUVLSVgyK0dDZ0VSN3NlTEFOMU5icmdka0hobnVoQmlZb0U4c2RRVUJoOEg3RWdSM2phQkZiUmcxZGZmaWJqOD0iLCJ0c19zaWduIjoidHMuMi45YzI1YzY5YTM0NDA5MjY2NTU0MmI2YzJmNTk4NDNiZDM1ZDJlYjRhN2UzODdlMDFjMzIzMjQyZmU5ZDAxZGU4YzRmYmU4N2QyMzE5Y2YwNTMxODYyNGNlZGExNDkxMWNhNDA2ZGVkYmViZWRkYjJlMzBmY2U4ZDRmYTAyNTc1ZCIsInJlcV9jb250ZW50Ijoic2VjX3RzIiwicmVxX3NpZ24iOiJhUFY3RWdjVmpoNC9CeEw0MlgxQ3lYRUFCTU03aUZsK3Q2bEN0ZmpOTENjPSIsInNlY190cyI6IiNHU1BJZkQ0VlJ4OEUxZ1VoUDJDekR6N1JOM3ZCdjdpSUl3bXg1c2RRdDgxTVpCNW1kMkJ5U0lsdnVsVWQifQ%3D%3D; publish_badge_show_info=%220%2C0%2C0%2C1780091507936%22; odin_tt=627229c3cbd2c64c97f07d9711144f4d94dc55e2246606edcc7ff137c2a2947fc748af355704bb3f16c78bca89b8fb8c318849c0a74de4b1e1e676cb1b7255e9",
    },
    "bili_cookie": {
        "type": "password", "label": "B站 Cookie", "group": "凭证", "advanced": True,
        "path": "bili_cookie",
        "help": "含 SESSDATA 的 B站 Cookie，用于绕过 412 风控；也可发「B站登录」扫码（扫码结果存在共享 cookie 池）",
        "default": "buvid3=F4A54186-79A6-9DF8-8804-B0B7B5F2162E63952infoc; b_nut=1788701463; bsource=search_sm; _uuid=A7898268-106D1-F8B10-EC4A-73B710C69915D65272infoc; bili_ticket=eyJhbGciOiJIUzI1NiIsImtpZCI6InMwMyIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3ODg5NjA2NjUsImlhdCI6MTc4ODcwMTQwNSwicGx0IjotMX0.-KKo76_ztIu28x67iE7MAbYtjtv2UQRTB2TJG8ZXTJI; bili_ticket_expires=1788960605; bmg_af_switch=1; bmg_src_def_domain=i2.hdslb.com; buvid_fp=5927570ba842a6cce21a645b14803179; buvid4=C3F4D1E7-EEFA-7362-3C62-8BA37A5E3C0465996-126090621-joqonVuSK2N4kYNOsFxgsSnl/8bDdHAa7EQDwnMVE8qJFEbCIBQZoWrfAwz/DW4+; SESSDATA=1e24b38b%2C1804253507%2Cf672b%2A91CjAUqqKS3z2e4AH5TS9uDdYYf6vG9JASN4RU1rUWFbUdRPdHLzuuoWmkEQJrJooBmuISVjdCYnp6SDFLQm9OUXhLUWItdmNtTW1hUFNBMGhyZXVYUkJTZmdBZ0xzODh4MldsanR6Umk5ZVhNRXdXWmVXX3pHVEs4Q1R6cHF1QTJkUzdBZVZielBnIIEC; bili_jct=9b35e918048d66e68adcdbd8227a3fa3; DedeUserID=275303303; DedeUserID__ckMd5=4bc1444d2336d7ad; sid=5mi29af6; b_lsid=727C5E8D_1A076EB08C6",
    },

    # ---------------- 历史遗留/核心兼容键（均未被使用或仅核心识别）----------------
    "auto_send_video": {
        "type": "bool", "label": "自动发送视频（未生效）", "group": "其它", "advanced": True,
        "path": "auto_send_video",
        "help": "插件会把它写回配置文件但当前解析核心不读取，保留兼容",
        "default": True,
    },
    "auto_send_images": {
        "type": "bool", "label": "自动发送图片（未生效）", "group": "其它", "advanced": True,
        "path": "auto_send_images",
        "help": "插件会把它写回配置文件但当前解析核心不读取，保留兼容",
        "default": True,
    },
    "merge_images": {
        "type": "bool", "label": "合并图片（未生效）", "group": "其它", "advanced": True,
        "path": "merge_images",
        "help": "配置文件里存在但当前解析核心不读取（是否合并转发由 forward_threshold 决定）",
        "default": True,
    },
    "max_images": {
        "type": "number", "label": "最大图片数（未生效）", "group": "其它", "advanced": True,
        "path": "max_images", "min": 1, "max": 200, "step": 1, "unit": "张",
        "help": "配置文件里存在但当前解析核心不读取，保留兼容",
        "default": 100,
    },
    "show_source": {
        "type": "bool", "label": "显示来源（未生效）", "group": "其它", "advanced": True,
        "path": "show_source",
        "help": "配置文件里存在但当前解析核心不读取，保留兼容",
        "default": True,
    },
    "at_reply": {
        "type": "bool", "label": "@触发者（未生效）", "group": "其它", "advanced": True,
        "path": "@_reply",
        "help": "配置里的键名是 @_reply（字段名不能带 @，故用 path 指回）；当前解析核心不读取",
        "default": True,
    },
    "parse_timeout": {
        "type": "number", "label": "解析总时长上限", "group": "参数", "advanced": True,
        "path": "parse_timeout", "min": 30, "max": 3600, "step": 30, "unit": "秒",
        "help": "解析+下载+合并+发送 的总时长。网络慢或视频大时必须放宽，否则会报「解析失败」；"
                "超时不会取消后台下载，因此还可能出现「先提示失败、稍后又发出视频」",
    },
    "download_parallel": {
        "type": "number", "label": "分段下载路数", "group": "参数", "advanced": True,
        "path": "download_parallel",
        "default": 8, "min": 1, "max": 16, "step": 1, "unit": "路",
        "help": "大文件改用多连接 Range 分段下载（1=单连接）。单连接被 CDN 限速时 8 路能快一个数量级；"
                "服务端不支持 Range 会自动回退单连接，断了只补那一段",
    },
    "cache_cleanup_enabled": {
        "type": "bool", "label": "自动清理解析缓存", "group": "缓存",
        "path": "cache_cleanup_enabled",
        "default": True,
        "help": "解析下载的中间文件只增不减（线上曾到 11GB），开启后按下面的天数/大小周期清理",
    },
    "cache_max_age_days": {
        "type": "number", "label": "缓存保留天数", "group": "缓存",
        "path": "cache_max_age_days",
        "default": 7, "min": 0, "max": 365, "step": 1, "unit": "天",
        "help": "超过这个天数的缓存会被删除；0 = 不按时间清理。最近 1 小时动过的文件永不删",
    },
    "cache_max_size_mb": {
        "type": "number", "label": "缓存大小上限", "group": "缓存",
        "path": "cache_max_size_mb",
        "default": 2048, "min": 0, "max": 102400, "step": 128, "unit": "MB",
        "help": "缓存总量超过上限时，从最旧的文件开始删到上限的 90%；0 = 不按大小清理",
    },
    "cache_cleanup_interval_hours": {
        "type": "number", "label": "清理间隔", "group": "缓存", "advanced": True,
        "path": "cache_cleanup_interval_hours",
        "default": 6, "min": 1, "max": 168, "step": 1, "unit": "小时",
        "help": "多久检查清理一次（启动后先等 90 秒再清第一次）",
    },
    "download_timeout": {
        "type": "number", "label": "下载超时（核心别名）", "group": "其它", "advanced": True,
        "path": "download_timeout", "min": 5, "max": 3600, "step": 5, "unit": "秒",
        "help": "解析核心 config.py 也会读这个键；与 browser_timeout 同时存在时以本键为准",
    },
    "source_max_size": {
        "type": "number", "label": "大小上限（核心别名）", "group": "其它", "advanced": True,
        "path": "source_max_size", "min": 1, "max": 4096, "step": 1, "unit": "MB",
        "help": "解析核心 config.py 也会读这个键；与 download_max_size 同时存在时以本键为准",
    },
    "common_timeout": {
        "type": "number", "label": "接口超时（核心别名）", "group": "其它", "advanced": True,
        "path": "common_timeout", "min": 5, "max": 600, "step": 5, "unit": "秒",
        "help": "解析核心请求各平台接口用的超时（知乎/小黑盒/ pixiv 等）",
    },
    "single_heavy_render_card": {
        "type": "bool", "label": "单媒体渲染卡片（核心）", "group": "其它", "advanced": True,
        "path": "single_heavy_render_card",
        "help": "解析核心的开关，默认关闭（暂未启用该功能）",
    },
    "require_at_in_group": {
        "type": "bool", "label": "群里必须@才解析（核心）", "group": "其它", "advanced": True,
        "path": "require_at_in_group",
        "help": "解析核心的开关；注意：插件侧的群聊过滤在核心之前，当前实际由分群开关控制",
    },
    "command_labels": {
        "type": "json", "label": "指令标签映射", "group": "其它",
        "path": "command_labels", "advanced": True, "readonly": True, "rows": 4,
        "help": "插件启动时自动写入的「触发词 → 指令中文名」映射（开启/关闭视频解析、B站登录）；只读",
        "default": {"开启视频解析": "开启视频解析", "关闭视频解析": "关闭视频解析", "B站登录": "B站登录", "b站登录": "B站登录"},
    },
}

# ===========================================

import json
import os
import random
import re
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from utils.api import send_message
from utils.config import get_bot_name, get_bot_qq, get_config
from utils.plugin_toggle import is_enabled as _pt_enabled, set_enabled as _pt_set
from utils.command_registry import CommandRegistry

# ========== 配置 ==========
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "video_parser_config.json")

DEFAULT_CONFIG = {
    "enabled": True,
    "auto_send_video": True,
    "auto_send_images": True,
    "max_images": 50,
    "@_reply": True,
    "show_source": True,
    # 新解析核心（astrbot_plugin_parser 复刻版）参数
    "browser_timeout": 60,            # 下载超时（秒）
    "parse_timeout": 900,             # 解析+下载+合并+发送 的总时长上限（秒）
    "download_timeout": 900,          # 单次下载超时（秒）；核心别名，本键优先
    "download_parallel": 8,           # 分段下载并发段数（1=单连接）
    # 解析缓存保留策略（中间文件只增不减，线上曾到 2338 个 / 11GB）
    "cache_cleanup_enabled": True,    # 自动清理
    "cache_max_age_days": 7,          # 保留天数（0=不限）
    "cache_max_size_mb": 2048,        # 总大小上限 MB（0=不限）
    "cache_cleanup_interval_hours": 6,
    "download_retry_times": 3,        # 下载重试次数
    "download_max_size": 300,         # 下载大小上限（MB）
    "download_max_duration": 3600,    # 视频时长上限（秒）
    "forward_threshold": 4,           # 消息段达到该数量强制合并转发
    "debounce_seconds": 30,           # 解析防抖间隔（秒）
    "show_download_fail_tip": True,   # 下载失败时提示
    "audio_to_file": False,           # 音频以文件形式发送（默认语音）
    "proxy": "",                      # 代理地址（如 http://127.0.0.1:7890）
    "bili_video_quality": "P_720P",   # B站画质：P_360P/P_480P/P_720P/P_1080P
    "bili_video_codec": "AVC",        # B站编码：AVC/HEVC/AV1
    "bili_cookie": "",                # B站Cookie(含SESSDATA，绕过412风控)
    # 开始解析的表情回应（QQ 长按消息 → 回应表情）：告诉用户"已经在解析了"
    "react_on_start": True,           # 触发解析时先给触发消息回一个表情
    "react_emoji_ids": [              # 表情池：每次从里面**随机抽一个**（QQ表情ID）
        128064,   # 👀 眼睛
        128269,   # 🔍 放大镜
        129300,   # 🤔 思考
        128170,   # 💪 加油
        128077,   # 👍 赞
        128079,   # 👏 鼓掌
        128640,   # 🚀 火箭
        10024,    # ✨ 闪光
    ],
    "react_remove_if_no_link": True,  # 兼容保留：现在只有真触发解析才回表情，不会误标
}

_CONFIG = {}


def vlog(level, msg):
    """视频解析 分级日志"""
    try:
        from utils.log import plugin_log
        plugin_log("视频解析", level, msg)
    except Exception:
        print(f"[视频解析] {msg}")


def _load_config():
    global _CONFIG
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                _CONFIG = json.load(f)
        except Exception:
            pass  # 解析失败保留旧配置
    else:
        _CONFIG = {}
    for k, v in DEFAULT_CONFIG.items():
        _CONFIG.setdefault(k, v)
    _save_config()


def _save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(_CONFIG, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def cfg(key, default=None):
    """读取配置（内存值，SIGUSR1 重载）"""
    return _CONFIG.get(key, default)


def reload_config():
    """Web 端保存配置后由主程序（SIGUSR1）通知调用"""
    _load_config()
    try:      # 面板改完"解析总时长上限"立刻生效
        from plugins.parser_bridge import configure_timeout as _t
        _t(cfg("parse_timeout", 900))
    except Exception:
        pass
    try:      # 分段路数也立刻生效（缓存清理的开关/阈值由清理线程每轮重读）
        from plugins.parser_bridge import configure_parallel as _p
        _p(cfg("download_parallel", 8))
    except Exception:
        pass


_load_config()


# ============ 指令注册表（集中定义，一眼可读）============
registry = CommandRegistry("视频解析")


def _cmd_enable(event, raw, kw):
    if event.get("message_type") == "group":
        _pt_set(event.get("group_id"), "video_parser", True)
        send_message(event, "视频解析已在本群开启")
    else:
        _CONFIG["enabled"] = True
        _save_config()
        send_message(event, "视频解析已开启")
    return True


def _cmd_disable(event, raw, kw):
    if event.get("message_type") == "group":
        _pt_set(event.get("group_id"), "video_parser", False)
        send_message(event, "视频解析已在本群关闭")
    else:
        _CONFIG["enabled"] = False
        _save_config()
        send_message(event, "视频解析已关闭")
    return True


def _cmd_bili_login(event, raw, kw):
    """B站扫码登录：生成二维码，手机B站APP扫码后自动保存凭证（绕过412风控）"""
    import asyncio
    import threading

    try:
        from plugins.parser_bridge import get_cache_dir, host_to_container
        from video_parser_core.config import PluginConfig as _PCfg
        from video_parser_core.parsers.bilibili.login import BilibiliLogin

        _pcfg = _PCfg()
        _pcfg.load_from_video_config(CONFIG_FILE)
        login = BilibiliLogin(_pcfg)

        loop = asyncio.new_event_loop()
        try:
            qr_bytes = loop.run_until_complete(login.login_with_qrcode())
        finally:
            loop.close()

        qr_path = os.path.join(get_cache_dir(), "bili_login_qr.png")
        with open(qr_path, "wb") as f:
            f.write(qr_bytes)
        send_message(
            event,
            [
                {"type": "image", "data": {"file": host_to_container(qr_path)}},
                {"type": "text", "data": {"text": "请用哔哩哔哩APP扫码登录（约2分钟内有效）"}},
            ],
        )

        def _poll_qr():
            try:
                async def _run():
                    async for state in login.check_qr_state():
                        send_message(event, state)
                _loop = asyncio.new_event_loop()
                try:
                    _loop.run_until_complete(_run())
                finally:
                    _loop.close()
            except Exception as e:
                vlog("error", f"B站扫码状态轮询异常: {e}")

        threading.Thread(target=_poll_qr, daemon=True).start()
        return True
    except Exception as e:
        vlog("error", f"B站扫码登录失败: {e}")
        send_message(event, f"B站登录失败: {e}")
        return True


# 注册指令：名称 / 触发词 / 描述 / 处理函数 / 权限 / 匹配方式
# ========== 解析缓存保留策略（按天数 + 按大小）==========
# 中间文件只增不减：线上实测曾到 2338 个文件 / 11GB（详见 video_parser_core/clean.py）
_cleaner = None


def _make_cleaner():
    """惰性创建清理器（要缓存目录 + 配置读取器）"""
    global _cleaner
    if _cleaner is None:
        try:
            from plugins.parser_bridge import get_cache_dir
            from video_parser_core.clean import CacheCleaner

            _cleaner = CacheCleaner(get_cache_dir(), get_config=lambda: _CONFIG)
        except Exception as e:
            vlog("warn", f"缓存清理器初始化失败: {e}")
    return _cleaner


def _run_cache_cleanup(dry_run=False):
    """跑一次清理（供后台线程与手动指令复用），返回统计"""
    c = _make_cleaner()
    if c is None:
        raise RuntimeError("缓存清理器不可用")
    st = c.clean_once(dry_run=dry_run)
    vlog("info", f"缓存清理{'（试算）' if dry_run else ''}：删除 {st['deleted']} 个文件 / "
                 f"释放 {st['freed_mb']} MB；剩余 {st['remaining_count']} 个 / "
                 f"{st['remaining_mb']} MB")
    return st


def _cmd_clean_cache(event, raw, kw):
    """主人指令：手动跑一次缓存清理"""
    try:
        st = _run_cache_cleanup()
        send_message(event, f"视频缓存已清理：删除 {st['deleted']} 个文件，释放 "
                            f"{st['freed_mb']} MB；剩余 {st['remaining_count']} 个 / "
                            f"{st['remaining_mb']} MB")
    except Exception as e:
        vlog("error", f"缓存清理失败: {e}")
        send_message(event, f"缓存清理失败: {e}")
    return True


registry.register("开启视频解析", ["开启视频解析"], "开启视频解析（群内=本群，私聊=全局）", _cmd_enable, master_only=True, kind="suffix")
registry.register("关闭视频解析", ["关闭视频解析"], "关闭视频解析（群内=本群，私聊=全局）", _cmd_disable, master_only=True, kind="suffix")
registry.register("B站登录", ["B站登录", "b站登录"], "B站扫码登录（生成二维码，手机B站APP扫码，绕过412风控）", _cmd_bili_login, master_only=True, kind="suffix")
registry.register("清理视频缓存", ["清理视频缓存"], "清理解析缓存（按保留天数与大小上限）", _cmd_clean_cache, master_only=True, kind="suffix")

# 同步指令中文名到配置（Web 面板展示可读指令名）
_CONFIG.setdefault("command_labels", {}).update(registry.labels())
_save_config()

# 启动解析缓存清理线程（守护线程；开关与阈值每次循环重读）
try:
    _c = _make_cleaner()
    if _c is not None:
        _c.start()
except Exception as _e:
    vlog("warn", f"缓存清理线程启动失败: {_e}")


# ========== 表情回应（告知"已开始解析"）==========
# 走 NapCat 的 set_msg_emoji_like：QQ 长按消息 → 回应表情。
# 注意用 POST + token 放 query（GET 传 set=false 会被序列化成字符串，可能被当成真）。
_REACT_OK = (0, 65002)   # 0=成功  65002=已经设置过该表情

_LINK_RE = re.compile(r"https?://\S+", re.I)


def on_parse_start(event):
    """解析核心确认命中受支持链接（即将开始下载）时回调 —— 给触发消息回一个表情。

    由核心在匹配成功后立即调用（早于下载/去水印），所以**只有真正触发解析才会有表情**，
    图片/表情包/语音等无关消息不会误触发。
    """
    if not cfg("react_on_start", True):
        return
    mid = (event or {}).get("message_id")
    if mid:
        react_to_message(mid)          # 从表情池随机抽一个


def _emoji_pool() -> list:
    """表情池：react_emoji_ids（列表）优先；兼容单值 react_emoji_id"""
    ids = cfg("react_emoji_ids")
    pool = []
    if isinstance(ids, (list, tuple)):
        for x in ids:
            try:
                pool.append(int(x))
            except (TypeError, ValueError):
                pass
    if not pool:                      # 兼容旧的单值配置
        single = cfg("react_emoji_id")
        try:
            if single not in (None, ""):
                pool = [int(single)]
        except (TypeError, ValueError):
            pool = []
    return pool or [128064]


def react_to_message(message_id, emoji_id=None, turn_on=True):
    """给触发消息加/撤表情回应。

    emoji_id=None 时从表情池**随机抽一个**；失败静默（不影响解析主流程）。
    返回实际使用的表情ID（成功）或 None（失败）——撤销时要用同一个ID。
    """
    try:
        import requests
        if emoji_id is None:
            emoji_id = random.choice(_emoji_pool())
        emoji_id = int(emoji_id)
        base = str(get_config("NAPCAT_HTTP", "http://127.0.0.1:3000")).rstrip("/")
        token = get_config("ACCESS_TOKEN", "")
        body = {"message_id": int(message_id), "emoji_id": emoji_id}
        if not turn_on:
            body["set"] = False
        r = requests.post(f"{base}/set_msg_emoji_like",
                          params={"access_token": token} if token else {},
                          json=body, timeout=5)
        d = r.json() if r.status_code == 200 else {}
        ok = d.get("status") == "ok" and (d.get("data") or {}).get("result", -1) in _REACT_OK
        vlog("debug", f"表情回应({'加' if turn_on else '撤'}) mid={message_id} "
                      f"emoji={emoji_id} -> {ok}")
        return emoji_id if ok else None
    except Exception as e:
        vlog("debug", f"表情回应异常: {e}")
        return False


# ========== 新解析核心接入（astrbot_plugin_parser 复刻版，16 平台）==========
try:
    from plugins.parser_bridge import (configure as _pb_configure,
                                       configure_timeout as _pb_timeout,
                                       configure_parallel as _pb_parallel,
                                       host_to_container, get_cache_dir)
    import video_parser_core.main as _vpm

    def _pb_send(event, segs):
        """核心解析结果 → CQ 段 → WS 发送（event 由核心传入）"""
        from plugins.parser_bridge import segs_to_cq
        import utils.api as _api
        try:
            _api.send_message(event, segs_to_cq(segs))
            return True
        except Exception as e:
            vlog("error", f"新核心发送失败: {e}")
            return False

    _pb_configure(_pb_send, host_to_container, on_parse_start)
    _pb_timeout(cfg("parse_timeout", 900))       # 解析总时长上限（慢速 CDN 上很关键）
    _pb_parallel(cfg("download_parallel", 8))    # 大文件分段下载路数
    vlog("info", f"超时设置：解析总时长 {cfg('parse_timeout', 900)}s / 单次下载 "
                 f"{cfg('download_timeout', 900)}s / 分段下载 {cfg('download_parallel', 8)} 路")

    # 预初始化解析核心（后台线程，避免首次消息卡顿）
    def _preinit_core():
        try:
            _vcfg = os.path.join(DATA_DIR, "video_parser_config.json")
            _vpm.get_plugin(send_fn=_pb_send, container_path_fn=host_to_container,
                            cache_dir=get_cache_dir(), video_config_path=_vcfg)
            vlog("info", "新解析核心已预初始化")
        except Exception as e:
            vlog("error", f"解析核心预初始化失败: {e}")
    threading.Thread(target=_preinit_core, daemon=True).start()

    # B站凭证到期每日提醒（共享 cookie 池：凭证失效 → 提醒主人重新扫码）
    def _send_master_private(text):
        try:
            masters = get_config("MASTER_QQ", [])
            if not isinstance(masters, list):
                masters = [masters]
            for m in masters[:3]:
                ev = {"post_type": "message", "message_type": "private", "user_id": int(m)}
                send_message(ev, text)
        except Exception:
            pass

    def _bili_expiry_check():
        import asyncio
        import time as _time
        while True:
            try:
                from video_parser_core.config import PluginConfig as _PCfg
                from video_parser_core.parsers.bilibili.login import BilibiliLogin
                _pcfg = _PCfg()
                _pcfg.load_from_video_config(os.path.join(DATA_DIR, "video_parser_config.json"))
                login = BilibiliLogin(_pcfg)
                if login.credential_file.exists():
                    _loop = asyncio.new_event_loop()
                    try:
                        cred = _loop.run_until_complete(login.credential)
                        expired = cred is None or not cred.has_sessdata()
                    finally:
                        _loop.close()
                else:
                    expired = True
                if expired:
                    _send_master_private("⚠️ B站登录已失效（共享Cookie池），请发「B站登录」重新扫码，否则B站视频解析会失败")
            except Exception as e:
                vlog("debug", f"B站凭证检查异常: {e}")
            _time.sleep(86400)

    threading.Thread(target=_bili_expiry_check, daemon=True).start()
except Exception as e:
    vlog("error", f"新解析核心接入失败: {e}")


def is_master(user_id):
    ml = get_config("MASTER_QQ", [])
    if not isinstance(ml, list):
        ml = [ml]
    return str(user_id) in [str(m) for m in ml]


def handle(event):
    if event.get("post_type") != "message":
        return False

    raw = event.get("raw_message", "").strip()
    uid = event.get("user_id", 0)

    # 主人开关指令（注册表统一分发）
    if registry.dispatch(event, raw, is_master(uid), master_cmds_only=True):
        return True

    if not cfg("enabled", True):
        return False
    if event.get("message_type") != "group":
        return False
    # 分群检查
    if not _pt_enabled(event.get("group_id"), "video_parser"):
        return False
    if not raw:
        return False

    # 表情回应由解析核心在"确认命中受支持链接"后回调触发（见 on_parse_start），
    # 这里只负责走解析流程。
    # 新解析核心：匹配链接并解析发送（16 平台，含 B站卡片/动态/专栏）
    try:
        from plugins.parser_bridge import handle_parse, configure_react
        configure_react(on_parse_start)      # 每次拿到新配置/新实例都重新注入
        result = handle_parse(event)
        # 2=成功(已发送) 1=未匹配链接 0=解析失败
        if result == 2:
            return True
        if result == 0:
            vlog("warn", "解析失败")
            send_message(event, "⚠️ 视频解析失败，请稍后再试")
            return True
        # result == 1：不是受支持的链接 → 从没回过表情，无需处理
    except Exception as _e:
        vlog("error", f"解析核心异常: {_e}")

    # 未匹配到支持的链接
    return False
