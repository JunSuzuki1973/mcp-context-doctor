# MCP Context Doctor

MCP設定を調べ、ツール定義によるコンテキスト負荷をローカルで計測するCLIと汎用Skillです。Codex、Claude Code、Cursor、VS Codeの設定に対応します。

[English](README.md) · [設計](docs/design.md) · [既存ツールの活用](docs/existing-tools.md)

接続数だけで危険度を判定しません。ツールを遅延ロードするホストは、ツール**名**だけを常に保持し、定義は使う時に取得します。そのためカタログのコストは単一の数値ではなく幅を持ちます。本ツールはその両端を報告します — **always loaded**（名前のみ＝下限）と **eager projection**（全説明文＋入力スキーマ＝上限）。実際の消費量はこの間にあり、測定しません。会話履歴、ツール出力、組み込み指示も別要因なので、オーバーフロー確率とは表現しません。

## 導入

Python 3.11以上とuvが必要です。

```sh
git clone https://github.com/JunSuzuki1973/mcp-context-doctor.git
cd mcp-context-doctor
uv sync --locked
uv run mcp-context-doctor --help
```

どのプロジェクトからでも呼び出す場合:

```sh
uv tool install "git+https://github.com/JunSuzuki1973/mcp-context-doctor.git@v0.2.0"
```

PyPIへは公開していません。公式MCP Python SDKとtiktokenを利用し、LLM APIキーなしで動きます。初回は依存関係・トークナイザデータをダウンロードします。

## 使い方

設定を読むだけの診断:

```powershell
mcp-context-doctor scan --host codex --project 'C:\work\my-project'
mcp-context-doctor scan --host claude-code --project 'C:\work\my-project' --format json
```

信頼済みのサーバーを指定して、接続・ツール定義を計測:

```powershell
mcp-context-doctor scan --host codex --project 'C:\work\my-project' --live --server my-server
```

認証が必要なエンドポイントを測定する場合は `--bearer-env` を使います。渡すのは**環境変数名**であってトークン本体ではありません（コマンドラインに書くとシェル履歴とプロセス一覧に残るため）。この指定はその実行限りで、設定ファイルは書き換えません。

```powershell
$env:GBRAIN_TOKEN = '...'
mcp-context-doctor scan --host claude-code --project 'C:\work\p' --live --server my-server --bearer-env GBRAIN_TOKEN
```

設定ファイル側に書く場合は `bearer_token_env_var`（トークンを保持する環境変数名）または `env_http_headers`（ヘッダー名→環境変数名の対応）が使えます。Doctor は OAuth フローを実行せず、資格情報ストアも読みません。したがって、ホストが対話的に認証して保持しているトークンは Doctor からは見えません。トークンが用意できない場合は、認証済みクライアントからカタログをエクスポートして `analyze` に渡してください。ツール名だけのカタログでも always-loaded の下限は測定できます。

`--server`には設定名または静的レポートのIDを使えます。`--live`は設定されたプログラムを起動したりURLへ接続します。サーバーの起動自体に副作用はあり得ますが、Doctorは業務ツールを呼び出しません。`--server`を省略すると、選択した設定の有効サーバーすべてが対象です。

保存済みデータの解析・差分比較:

```sh
mcp-context-doctor analyze examples/tools-list.json --format json --output report.json
mcp-context-doctor diff before.json after.json
```

`--context-window`と`--reserve`で予算を設定できます。`--fail-on-budget`はその仮定を超えると終了コード3を返します。共通の「危険な接続数」は設定していません。

## 汎用Skill

`skills/mcp-context-doctor`をフォルダーごと、Codexなら `~/.codex/skills/`、Claude Codeなら `~/.claude/skills/`へコピーしてください。CLIは別途導入します。GitHub ReleaseにはSkill単体ZIPもあります。

- Codex: `$mcp-context-doctor で、このプロジェクトのMCP構成を診断してください`
- Claude Code: `/mcp-context-doctor このプロジェクトのMCP構成を診断してください`

## 診断結果の読み方

レポートは冒頭で「判定」「その根拠となる測定範囲」「対応すべき項目」を、測定された影響度順に提示します。

各項目は、そのホストの遅延ロード状態・会話履歴・実際に使っているツールを知らなくても成立する根拠だけで構成しています。具体的には、少数のツールにコストが集中している、説明文がこの実行で測定した中央値から大きく外れている、同一バックエンドが複数エントリから参照されている、説明文が無いツールがある、測定できなかったサーバーがある、の5種類です。

閾値は `diagnose.py` の定数として明示した**レビュー用のヒューリスティック**であり、モデルやホストの制限値ではありません。**どの項目もコンテキストオーバーフローを主張しません。** また `nothing_measured`（何も測定できなかった）は「問題なし」ではありません。

## 結果の読み方と対応範囲

標準ではサーバー名・ツール名を匿名化したIDで表示します。ローカルで対応先を確認する場合は `--include-names` を使ってください。生の設定・認証情報・ツール説明本文はレポートに含めません。既存レポートの上書きは拒否します。

`always_loaded_tokens`（ツール名の合計）は、ホストが遅延ロードしていても必ず払う下限です。`eager_projection_tokens` は全定義を読み込む場合の上限で、入力スキーマを含まないキャプチャでは `null`（不明）になります — ゼロではありません。全件ロード推定と、出力スキーマ等を含むカタログ計測値は重複するため合算しません。tiktokenの値はClaude独自の正確なトークン数ではありません。失敗した接続は「未計測」でありゼロ扱いしません。

各設定ファイル・スコープは独立して報告します。複数ホストを足して一つのコンテキスト量とは扱えません。プラグイン有効状態、管理者設定、設定優先順位、OAuth保存領域、実際にロードされたツール集合は自動解決していません。明示的な `--config` でエクスポート済み設定やプラグインの `.mcp.json` を指定できます。

終了コード0はコマンドの完了、2は入力不備・ライブ計測の不完全、3は指定した推定予算の超過です。0だけで「PCが安全」とは判断しません。

`auth_required` は、そのエンドポイントが未認証のリクエストを拒否したという意味です。DoctorはOAuth認証を行いません。認証情報は設定ファイルが参照する環境変数から渡してください。エラーは固定コードのみを表示し、設定値やサーバー応答が漏れないようにしています。ローカルで原因を追う場合は `DOCTOR_DEBUG=1` を設定するとトレースバックが出ます。

公式MCP Inspector 2.5.0との相互確認、旧仕様STDIO・現行HTTP接続を含むテストを実施しています。詳細は[検証記録](docs/validation.md)をご覧ください。

MITライセンス。MCP、OpenAI、Anthropicの公式製品ではありません。
