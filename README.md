# SHCH Discord Bot

這是一個以 discord.py 2.x 建立的校園 Discord 機器人，目標是將國立中興大學附屬高級中學網站上的公開公告，自動同步到 Discord 論壇頻道。

## 功能重點

- 從學校公告 widget JSON API 抓取最新公告
- 以 SQLite 保存已看過與已發佈的公告，避免重複發文
- 新公告自動建立 Discord Forum 貼文
- 依公告類別套用 Forum 標籤，失敗時使用 fallback 標籤
- 支援手動 backfill、dry run、最新公告查詢與關鍵字搜尋
- 使用繁體中文作為使用者可見文案
- 不抓取或儲存成績、缺曠、密碼、cookies 或其他私人資料

## 專案結構

```text
.
  school_discord_bot/
    bot.py
    config.py
    cogs/
      announcements.py
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
- Manage Threads
- Manage Channels

只有在你要使用 /news sync_tags 自動建立或更新論壇標籤時，才需要 Manage Channels。

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
- /news check
  立即抓取並同步最新公告。
- /news backfill count:int
  補發最新 N 筆公告，預設 5，最多 30。
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

## 測試

```bash
.venv/Scripts/python.exe -m pytest tests
```

目前測試涵蓋：

- parser
- dedup/hash
- tag mapping

## 已知限制

- 學校網站若暫時無法存取，bot 會記錄錯誤並保留運作，不會整體崩潰。
- 目前不處理登入保護內容，也不會碰觸成績與缺曠等私人頁面。
- 訂閱通知、關鍵字 watch、行事曆抓取已預留資料表，但尚未啟用。