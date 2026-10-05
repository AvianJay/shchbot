# SHCH Discord Bot

這是一個以 discord.py 2.x 建立的校園 Discord 機器人，目標是將國立中興大學附屬高級中學網站上的公開公告，自動同步到 Discord 論壇頻道。

## 功能重點

- 從學校公告 widget JSON API 抓取最新公告
- 以 SQLite 保存已看過與已發佈的公告，避免重複發文
- 新公告自動建立 Discord Forum 貼文
- 依公告類別套用 Forum 標籤，失敗時使用 fallback 標籤
- 支援手動 backfill、dry run、最新公告查詢與關鍵字搜尋
- 使用繁體中文作為使用者可見文案
- 管理員設定學測倒數語音頻道，每天台灣時間 00:00 自動更新名稱
- 匿名版：學生透過面板按鈕分類投稿、可附圖片，管理員可設定是否審核後才發布
- 不抓取或儲存成績、缺曠、密碼、cookies 或其他私人資料

## 專案結構

```text
.
  school_discord_bot/
    bot.py
    config.py
    cogs/
      announcements.py
      anonymous_board.py
      countdown.py
      school_links.py
      admin.py
    services/
      school_news_client.py
      announcement_parser.py
      forum_poster.py
      tag_mapper.py
    db/
      database.py
      migrations.py
    models/
      announcement.py
      anonymous_board.py
  tests/
    fixtures/
      sample_news_page.html
      sample_news_detail.html
    test_announcement_parser.py
    test_dedup.py
    test_tag_mapper.py
  data/
  .env.example
  requirements.txt
  README.md
  Dockerfile
```

## 必要權限

Bot 至少需要以下 Discord 權限：

- View Channels
- Send Messages
- Create Public Threads / Send Messages in Threads
- Embed Links
- Attach Files
- Manage Threads
- Manage Channels

使用 /news sync_tags 自動建立或更新論壇標籤，或 /school countdown 更新語音頻道名稱時，需要 Manage Channels。倒數語音頻道也必須允許 bot 檢視頻道。

匿名版的頻道權限（/anon setup 會逐項檢查）：

- 匿名頻道：View Channels、Send Messages、Embed Links、Attach Files、Create Public Threads、Manage Threads（下架時刪除留言討論串）
- 後台頻道：View Channels、Send Messages、Embed Links、Attach Files
- 審核者：需要後台頻道的 Manage Messages 權限才能按「通過／拒絕／下架」

## 環境變數

請建立 .env，內容可從 .env.example 複製：

```env
DISCORD_TOKEN=
GUILD_ID=
ANNOUNCEMENT_FORUM_CHANNEL_ID=
POLL_INTERVAL_SECONDS=600
SCHOOL_HOME_URL=https://www.dali.tc.edu.tw/home
SCHOOL_NEWS_WIDGET_URL=https://www.dali.tc.edu.tw/ischool/widget/site_news/main2.php?allbtn=0&maximize=1&uid=WID_0_2_377afa59cce9f22276e3f66e9d896cb97110c95d
DATABASE_PATH=data/bot.sqlite3
DRY_RUN=false
ALLOW_INSECURE_SCHOOL_SSL_FALLBACK=true
ANNOUNCEMENT_MENTION_EVERYONE=false
ANNOUNCEMENT_MENTION_USERS=false
ANNOUNCEMENT_MENTION_ROLE_IDS=
ANNOUNCEMENT_MENTION_TEXT=
```

`ALLOW_INSECURE_SCHOOL_SSL_FALLBACK` 是給這個學校站台憑證相容性問題用的受控 fallback。當 Python/OpenSSL 無法驗證學校網站憑證時，bot 只會對同一個學校主機重試一次不驗證的 HTTPS 連線。

`ANNOUNCEMENT_MENTION_EVERYONE` 控制公告貼文是否允許 `@everyone` / `@here`。
`ANNOUNCEMENT_MENTION_USERS` 控制公告貼文是否允許直接 mention 使用者。
`ANNOUNCEMENT_MENTION_ROLE_IDS` 可填逗號分隔的角色 ID 名單，只允許這些角色在公告初始訊息中被 mention。
`ANNOUNCEMENT_MENTION_TEXT` 可填自訂公告前綴，例如 `@everyone`、`<@&1234567890>` 或「新公告來了」。若留空，bot 會自動用 `ANNOUNCEMENT_MENTION_EVERYONE` 與 `ANNOUNCEMENT_MENTION_ROLE_IDS` 產生 mention 前綴。
只有公告日期是今天或昨天（台灣時間）的貼文才會加上這個前綴；補發的舊公告照常發文但不 mention，避免一次 tag 好幾十次。

注意：不要提交 .env，也不要在 log 中輸出 token。

## 本機執行

1. 建立並啟用 Python 3.11+ 環境。
2. 安裝套件：

```bash
pip install -r requirements.txt
```

3. 填好 .env。
4. 在 d:/shchbot 這個工作區根目錄啟動 bot：

```bash
.venv/Scripts/python.exe -m school_discord_bot
```

## Docker 執行

在 d:/shchbot 工作區根目錄執行：

```bash
docker build -t school-discord-bot .
docker run --env-file .env school-discord-bot
```

如果要把資料庫持久化，請額外掛載 data 目錄。

## Slash 指令

### 管理員指令

- /school setup
  驗證 guild、forum channel、bot 權限、資料庫與 scraper 狀態。
- /school countdown channel:語音頻道 exam_date:YYYY-MM-DD
  設定學測倒數的語音頻道及學測第一天日期。設定後立即更名為「學測倒數 {day} 天」，之後每天台灣時間（UTC+8）00:00 更新。設定保存於 SQLite，重啟或重新連線時會補更新；考試當天及之後顯示 0 天。再次執行可更換頻道或日期。此指令限具有「管理伺服器」或「管理頻道」權限的管理員使用。
- /anon setup public_channel:文字頻道 review_channel:文字頻道 require_review:bool
  設定匿名版的匿名頻道（公開發布）、後台頻道（審核與紀錄，會顯示投稿者）與是否需要審核，`require_review` 預設為是。兩個頻道不能相同，後台頻道不能讓 @everyone 看到。首次設定會建立預設分類。再次執行可更換設定。
- /anon send_panel
  在目前頻道發送匿名投稿面板，按鈕在 bot 重啟後仍可使用。
- /anon category add name / remove name / list
  管理投稿分類（最多 25 個），名稱可直接包含 emoji，例如「😡 我要靠北」。刪除分類不影響已發布的投稿。
- /anon status
  查看匿名版設定、分類與待審核數量。

所有 /anon 指令限具有「管理伺服器」或「管理頻道」權限的管理員使用。
- /news check
  立即抓取並同步最新公告（範圍同背景輪詢：全部置頂公告＋最新 30 篇非置頂公告）。
- /news backfill count:int
  往回檢查最新 N 篇非置頂公告（置頂公告一律檢查），只補發還沒發過的，預設 50，最多 100。補發順序由舊到新。
- /news status
  顯示上次檢查時間、最後發文公告、資料庫數量與論壇頻道。
- /news sync_tags
  建立或同步預設公告類別標籤。
- /news tag_map category tag
  手動將學校類別對應到現有論壇標籤。
- /news dry_run count:int
  預覽將要發送的公告，不實際發文。

### 一般使用者指令

- /news latest count:int category:str unit:str keyword:str
  查詢最近的公告。
- /news search keyword:str
  搜尋已保存的公告。
- /school links
  顯示學校常用公開連結按鈕。
- /school help
  顯示指令說明。

## 公告同步流程

1. 先抓取 widget 設定與列表 JSON。
2. 依 canonical URL 或內容 hash 去重。
3. 取得詳情 JSON，必要時 fallback 到詳情 HTML。
4. 解析內文、附件、外部連結與可能重要日期。
5. 寫入 SQLite。
6. 建立 Discord Forum 貼文並保存 thread ID。

## 預設類別標籤

預設會優先同步以下類別：

- 一般公告
- 競賽資訊
- 課程活動
- 大學升學
- 新生入學
- 獎助學金
- 榮譽事蹟
- 研習活動
- 自主學習
- 學習歷程

另外會同步一小組高頻單位標籤，避免超過 Discord forum 最多 20 個 tags 的限制：

- 訓育組
- 設備組
- 教學組
- 註冊組
- 實研組
- 圖書館
- 衛生組
- 輔導室
- 試務組

其他單位仍只會保留在 embed 欄位中，不會自動新增 tag。

## 匿名版

1. 管理員執行 `/anon setup` 設定頻道與審核模式，再用 `/anon send_panel` 發送投稿面板。
2. 學生按「✍️ 匿名投稿」，在 modal 中選擇分類、填寫內容（最多 2000 字），可選擇附最多 4 張圖片（JPG、PNG、GIF、WEBP）。每人每 5 分鐘可投稿一次。
3. 每則投稿都會先送到後台頻道，附上投稿者身分與圖片：
   - 需要審核：後台出現「通過／拒絕」按鈕，通過後才發布到匿名頻道。
   - 不需審核：立即發布；若發布失敗，投稿會留在後台等待人工處理。
4. 發布的貼文標題為「#編號 分類」（例如 `#12 😡 我要靠北`），並自動開一個討論串供留言。編號在發布時才分配，被拒絕的投稿不佔編號。
5. 已發布的貼文可在後台按「下架」，會連同討論串一起刪除。
6. 通過、拒絕（可附理由）與下架都會私訊通知投稿者，私訊不會透露是哪位管理員處理的。

隱私說明：

- 其他同學看不到投稿者；後台頻道會顯示投稿者，請只開放給管理員。面板與投稿視窗都有告知學生這一點。
- 公開貼文由 bot 直接發送，不帶任何投稿者資訊；內容中的 mention 會被跳脫，不會 tag 到任何人。
- 圖片會重新編碼並移除 EXIF（含 GPS 位置）、相機資訊與註解，檔名改為 `anon_編號_序號`。動畫圖片（GIF、APNG、動態 WEBP）無法重新編碼，會原樣轉發。

## 測試

```bash
.venv/Scripts/python.exe -m pytest tests
```

目前測試涵蓋：

- parser
- dedup/hash
- tag mapping
- 匿名版（投稿、審核、併發處理、權限檢查、匿名性、圖片中繼資料清除）

## 已知限制

- 學校網站若暫時無法存取，bot 會記錄錯誤並保留運作，不會整體崩潰。
- 目前不處理登入保護內容，也不會碰觸成績與缺曠等私人頁面。
- 訂閱通知、關鍵字 watch、行事曆抓取已預留資料表，但尚未啟用。
