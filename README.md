# Agent-ready Web SDK v0.4

既存 EC カタログを Agent から読み取るための FastAPI + MCP SDK。
`discover` / `search` / `quote` を提供し、**購入・決済・在庫予約は実装しません**。
Python 3.11 以上。

## Windows / PowerShell で起動

```powershell
cd path\to\agent-ready-web
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 認証キーは一度生成し、秘密管理ツール等へ安全に保管してください。
$env:AGENT_API_KEY = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))")
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

仮想環境の Activate.ps1 は不要です。デフォルトはデモカタログ。
Linux は `.venv/bin/python` に置き換えます。
キーは32文字以上の空白を含まない ASCII。サーバー側の環境変数で渡し、ブラウザーのコードや Git に含めないでください。
認証なしの既存デモ利用も互換目的で維持しますが、これはローカルの開発・テスト専用です。
Shopify モードでは `AGENT_API_KEY` がなければ起動に失敗します。
外部公開時は HTTPS の認証付きゲートウェイを使用し、デモを公開しないでください。

```powershell
$headers = @{ Authorization = "Bearer $env:AGENT_API_KEY" }
Invoke-RestMethod http://127.0.0.1:8000/discover -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/search?q=usb&limit=20' -Headers $headers
Invoke-RestMethod http://127.0.0.1:8000/quote -Method Post -Headers $headers `
  -ContentType 'application/json' -Body '{"product_id":"sku-001","quantity":2}'
```

`/docs`、`/openapi.json` も認証対象です（ブラウザーの直接アクセスでは401になります）。
`/health` は認証不要ですが、外部サービスへの接続状態までは確認しません。
数量は整数1〜100、検索語は200文字以下、limit は1〜100、商品IDは1〜256文字です。
API の入力エラーは422、認証エラーは401、制限超過は429、カタログ取得失敗は502。
見積もりの商品IDには検索結果の **バリエーションID** を指定します。

## Shopify 店舗への接続

店舗所有者が承認したアプリの **Admin GraphQL API** を使用します。

1. [Shopify Dev Dashboard](https://dev.shopify.com/dashboard) の **Stores → Create store → Dev** で開発用ストアを作成します。既存の開発用ストアを使う場合は、アプリと同じ組織に表示されることを確認します。[開発用ストアの作成手順](https://shopify.dev/docs/apps/build/stores/development-stores)
2. 同じ組織で **Apps → Create app → Start from Dev Dashboard** を選びます。**Versions** で `read_products`、`read_inventory`、`read_publications` を指定したバージョンを **Release** します。**Install app** から開発用ストアを選び、読み取り権限を承認します。インストール後に権限を変更した場合は、店舗管理画面で新しい権限を承認します。[アプリ作成とインストール](https://shopify.dev/docs/apps/build/dev-dashboard/create-apps-using-dev-dashboard)
3. 店舗管理画面の **商品管理 → インポート** で `shopify_test_products.csv` を読み込みます。公開7商品と非公開・下書き・アーカイブ各1商品を確認します。商品CSVの在庫数は単一ロケーション向けです。複数ロケーションの場合は在庫CSVで数量を設定します。JPYで検証する場合はストア基本通貨をJPYに設定します。[商品CSV仕様](https://help.shopify.com/ja/manual/products/import-export/using-csv)
4. アプリの **Settings → Credentials** で Client ID と Client secret を確認します。下記の PowerShell の入力欄か、組織で承認済みの秘密管理ツールから渡します。[Shopifyの認証手順](https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant)

バージョン作成時に App URL の入力を求められた場合は、Shopify が案内する `https://shopify.dev/apps/default-app-home` を使用できます。

承認済み Admin GraphQL エクスプローラー等で次を実行し、対象販売チャネルの Publication ID を確認します。

```graphql
query {
  publications(first: 20, catalogType: APP) {
    nodes { id name }
    pageInfo { hasNextPage endCursor }
  }
}
```

チャネルが20件を超える場合は `after: "endCursorの値"` を加えて続行します。
返された `gid://shopify/Publication/...` のうち、Agent へ公開するチャネルを指定します。
アプリにそのチャネルの公開情報を読む権限がない場合はエラーとなります。

```powershell
$env:CATALOG_ADAPTER = 'shopify'
$env:SHOPIFY_SHOP = 'your-store.myshopify.com'
$env:SHOPIFY_PUBLICATION_ID = 'gid://shopify/Publication/123456789'
$env:SHOPIFY_API_VERSION = '2026-10'
$env:AGENT_API_KEY = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))")
# Client Credentials Grant を選ぶ場合は、現在のセッションの固定トークン設定を解除します。
Remove-Item Env:SHOPIFY_ADMIN_TOKEN -ErrorAction SilentlyContinue
$env:SHOPIFY_CLIENT_ID = [System.Net.NetworkCredential]::new('', (Read-Host 'Shopify Client ID' -AsSecureString)).Password
$env:SHOPIFY_CLIENT_SECRET = [System.Net.NetworkCredential]::new('', (Read-Host 'Shopify Client secret' -AsSecureString)).Password
# 確認コマンド用に同じ設定を引き継ぐ PowerShell を開きます。
Start-Process powershell.exe -WorkingDirectory (Get-Location).Path
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

`SHOPIFY_ADMIN_TOKEN` を使う場合は、上の Client ID / Client secret の入力2行を
`$env:SHOPIFY_ADMIN_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Shopify Admin token' -AsSecureString)).Password`
に置き換えます。両方式を設定した場合は固定トークンが優先されます。固定トークンのローテーションは運用側で行います。
認証情報は現在のセッションとその子プロセスに渡ります。チャット、コマンド履歴、リポジトリ、共有設定には記載せず、検証後は関連する PowerShell を閉じます。

`SHOPIFY_SHOP` は正確な `*.myshopify.com` のホスト名のみです。
接続先は `https://<shop>/admin/api/<version>/graphql.json`。
TLS 検証は有効で、リダイレクトへトークンを転送しません。
Client Credentials Grant は初回のカタログ取得時に同じ店舗の `/admin/oauth/access_token` でトークンを取得します。
Shopify の `expires_in` に従い、最大60秒前に更新します。同一プロセス内の並行リクエストでトークンを共有します。
トークンと認証情報はレスポンス・監査ログに含めません。
ネットワーク制限のある環境では、対象店舗のホストへの HTTPS を許可してください。

- `ACTIVE` かつ指定 Publication の `publishedOnPublication == true` の商品のみを返します。
  不明・未公開・下書き・アーカイブ済みは除外します。
- 商品とバリエーションの両方を終端までページングします。循環・欠損カーソルはエラーです。
  ネストした Admin GraphQL クエリのコストを抑えるため商品・初回バリエーションは20件ずつです。
- バリエーションごとの価格と店舗基本通貨を使用します。Markets の国別価格、税・送料・割引は計算しません。
- 価格不明は `price` / `unit_price` / `total_price: null`。通貨不明も `null`。
  在庫欠損・追跡無効は `stock: null` / `available: null`。不明をゼロ扱いしません。
  負の在庫は販売可能在庫ゼロとして扱います。バックオーダーポリシーや配送先別の販売可否は評価しません。
- `quote` は毎回カタログを取得した時点の参考値で、有効期限5分、非拘束・予約なしです。
- HTTPタイムアウトは各通信フェーズ12秒、ネットワークエラー・429・一時的5xx・GraphQL THROTTLED は最大3回再試行。
  Retry-After と GraphQL の復元レートを考慮し、待機は1回最大60秒。
  Client Credentials Grant で GraphQL が401を返した場合は、新しいトークンでそのリクエストを1度だけ再送します。
  固定トークンの401・権限エラー・不正レスポンスは再試行しません。価格・在庫・通貨のフィールド単位エラーで
  商品データが残る場合は該当値を `null` にします。それ以外の GraphQL エラーや、権限不足でクエリ全体が拒否された場合は502です。

## MCP / Agent から利用

MCP は公式 Python SDK の **stdio** トランスポートです。HTTP MCP / SSE サーバーは公開しません。
FastAPI を別途起動する必要はなく、同じ `create_app` のビジネスロジックを再利用します。

```powershell
.\.venv\Scripts\python.exe -m app.mcp_server
```

このコマンドは通常、MCP クライアントから子プロセスとして起動します。
stdout は MCP 専用、監査イベントは stderr に出力されます。
クライアント設定例（パスを実際の絶対パスへ変更）:

```json
{
  "mcpServers": {
    "agent-ready-web": {
      "command": "C:\\projects\\agent-ready-web\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"],
      "cwd": "C:\\projects\\agent-ready-web"
    }
  }
}
```

利用するクライアントが `cwd` をサポートすることを確認してください。
環境変数は起動する親プロセスの安全な環境設定から継承させます。クライアントごとに継承仕様を確認し、
キーを共有設定ファイルへ埋め込まないでください。stdio のアクセス権限は OS・親プロセスを信頼境界とし、
Shopify のキー設定検証も実施します。stdio をネットワークへ中継する場合の認証は本バージョンの対象外です。

ツールは `discover {}`、`search {"q":"usb","limit":5}`、
`quote {"product_id":"検索結果のid","quantity":2}` の3つだけです。
read-only のヒントを宣言し、異常時は MCP の `isError` を返します。
公式クライアントで3機能を順に呼ぶデモ例:

```powershell
.\.venv\Scripts\python.exe -m examples.mcp_client
```

### 開発用ストアでの確認

認証情報を設定した環境でのみ実行します。REST 側で `data_source` が `ShopifyAdapter` と表示され、検索に続いて見積が成功することを確認します。
指定 Publication がオンラインストアの場合、今回のCSVでは公開7商品・15バリエーションが対象です。

~~~powershell
$headers = @{ Authorization = "Bearer $env:AGENT_API_KEY" }
$base = 'http://127.0.0.1:8000'
Invoke-RestMethod "$base/discover" -Headers $headers
$catalog = Invoke-RestMethod "$base/search?limit=100" -Headers $headers
$catalog.items | Format-Table id, name, price, currency, stock
foreach ($title in @('卓上デジタルマルチメーター', '試作電流センサー', '旧型圧着工具')) {
    $query = [uri]::EscapeDataString($title)
    if ((Invoke-RestMethod "$base/search?q=$query" -Headers $headers).total -ne 0) {
        throw "Unexpected publication: $title"
    }
}
foreach ($voltage in @('12 V', '24 V')) {
    $query = [uri]::EscapeDataString("DINレール直流電源 / $voltage")
    $matchesForVariant = (Invoke-RestMethod "$base/search?q=$query" -Headers $headers).items
    if (@($matchesForVariant).Count -ne 1) { throw "Expected one variant for $voltage" }
    $body = @{ product_id = $matchesForVariant[0].id; quantity = 2 } | ConvertTo-Json -Compress
    Invoke-RestMethod "$base/quote" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
    .\.venv\Scripts\python.exe -m examples.mcp_client --query "DINレール直流電源 / $voltage" --quantity 2
    if ($LASTEXITCODE -ne 0) { throw "MCP check failed for $voltage" }
}
~~~

| 確認項目 | 期待値 |
|---|---|
| 12 Vを2個 | 在庫18、単価4200、合計8400、`available: true` |
| 24 Vを2個 | 在庫0、単価4600、合計9200、`available: false` |
| 公開範囲 | 卓上デジタルマルチメーター、試作電流センサー、旧型圧着工具は検索対象外 |
| 通貨・見積条件 | 店舗の基本通貨、`binding: false`、`inventory_reserved: false`、有効期限約5分 |

テストCSVがリポジトリの親フォルダーにある場合は、認証設定済みの PowerShell で
`.\.venv\Scripts\python.exe -m examples.verify_shopify_store` を実行すると、RESTとMCPの結果をまとめて確認できます。
CSV外の公開サンプルは一覧に表示し、CSVの公開予定商品に欠落がなく、非公開・下書き・アーカイブの3商品が両方の検索から除外されることを確認します。

## セキュリティ・運用上の範囲

認証は `Authorization: Bearer <AGENT_API_KEY>`。キーはサーバー環境で変更してプロセスを再起動すると更新されます。
認証情報は URL や検索語に渡さないでください。
監査ログは操作名・結果のみで、商品ID・検索語・リクエスト本文・認証ヘッダー・上流レスポンスを記録しません。
Uvicorn は上記の `--no-access-log` で起動し、プロキシや集中ログでも Authorization と URL クエリを記録しない設定にしてください。
監査ログは運用側で収集・保存・アクセス管理を行ってください。

制限はデフォルト毎分60回。`AGENT_RATE_LIMIT` で変更できます。
HTTP は接続元IP、MCP は stdio セッションのプロセス単位で制限します。
任意の X-Forwarded-For は信頼しません。プロキシ経由の接続元が同一なら制限も共有します。
これは単一プロセス向けのメモリー内制限で、複数ワーカーでの集約・複数店舗・ユーザー別権限は対象外です。
大規模店舗は全件取得の遅延が大きいため、実運用の次段階ではキャッシュ・同期処理・共有レート制限が必要です。

## 自動テスト

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

GitHub Actions は push・プルリクエスト・手動実行時に Windows の Python 3.11 / 3.12 でテストします。
CI では模擬応答を使います。Shopify の認証情報は不要です。
Shopify は httpx モックで公開状態、1,000件超の商品、100件超のバリエーション、通貨・在庫・価格不明、
ページング異常、タイムアウト、再試行とエラー非開示を検証します。
Client Credentials Grant はトークン交換、期限前更新、並行アクセス、401後の再取得、失敗時の非開示を検証します。
MCP は公式クライアントが実際の子プロセスと initialize / tools/list / tools/call を実行します。
実店舗・Windows 実機での検証結果は [開発レポート](DEVELOPMENT.md) を参照してください。
