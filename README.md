# GitHub Traffic Dashboard

GitHub のトラフィック（Insights → Traffic）は直近 14 日分しか見られません。
このアプリは毎日トラフィックを取得して DynamoDB に積み上げ、累計を CloudFront 上のダッシュボードで見られるようにします。

- 追跡対象は「実行時点で public な、自分がオーナーのリポジトリ」です。毎回一覧を取り直すので、新しく作ったリポジトリや private から public に切り替えたリポジトリも、次の実行から自動で対象になります
- public でなくなったリポジトリは収集を止め、それまでの履歴は残します
- リポジトリはリネームしても履歴がつながるよう、名前ではなく ID で記録しています

## 構成

```
EventBridge Scheduler (毎日 09:30 JST)
  └─ Lambda (Python) ── GitHub REST API
       ├─ DynamoDB  日別のビュー・クローン、参照元・人気ページのスナップショット
       └─ S3        data/summary.json（ダッシュボード用の集計）
CloudFront ─ CloudFront Functions (Basic 認証、資格情報は KeyValueStore)
  └─ S3      index.html + data/summary.json
```

| 取得するもの | API | 保存のしかた |
|---|---|---|
| ビュー数・ユニーク訪問者 | `GET /repos/{owner}/{repo}/traffic/views` | 日別。既存値より大きければ上書き |
| クローン数・ユニーククローン | `GET /repos/{owner}/{repo}/traffic/clones` | 同上 |
| 参照元 | `GET /repos/{owner}/{repo}/traffic/popular/referrers` | 直近 14 日の集計を日ごとのスナップショットとして保存 |
| 人気ページ | `GET /repos/{owner}/{repo}/traffic/popular/paths` | 同上 |

ユニーク数は日別の値しか取れないため、ダッシュボードの「ユニーク」は日別ユニーク数の合計です。期間全体の実人数ではありません。

## 必要なもの

- Node.js 20 以上、AWS CDK v2（`npm install` で入ります）
- AWS 認証情報（CDK bootstrap 済みのアカウント）
- GitHub の fine-grained personal access token

## セットアップ

### 1. GitHub トークンを作る

次の URL を開くと、名前と権限が入った状態で作成画面が開きます（`target_name` は自分のユーザー名に置き換えてください）。

```
https://github.com/settings/personal-access-tokens/new?name=github-traffic-dashboard&description=Read+traffic+of+public+repositories&target_name=<YOUR_USER>&expires_in=366&administration=read
```

- Repository access は **All repositories** を選びます。ここを特定リポジトリに絞ると、新しく作ったリポジトリを追跡できません
- Repository permissions は **Administration: Read-only** だけで足ります（Metadata: Read-only は自動で付きます）。Traffic API がこの権限を要求するためです（[公式ドキュメント](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)）

有効期限は最長 366 日です。切れると収集が止まるので、期限前に作り直して手順 3 をやり直してください。

### 2. デプロイ

```bash
npm install
npx cdk deploy
```

既定のリージョンは `ap-northeast-1` です。AWS プロファイルの既定リージョンではなく、環境変数 `DEPLOY_REGION` で変えます（例: `DEPLOY_REGION=us-east-1 npx cdk deploy`）。以降の `aws` コマンドも同じリージョンで実行してください。

### 3. GitHub トークンを SSM Parameter Store に入れる

トークンを履歴に残さないよう、`read -s` で入力します。

```bash
read -rs GITHUB_TRAFFIC_TOKEN && export GITHUB_TRAFFIC_TOKEN
./scripts/put-github-token.sh
unset GITHUB_TRAFFIC_TOKEN
```

1Password CLI を使う場合は `.env.op` の参照先を自分の項目に合わせて、次のように実行できます。

```bash
op run --env-file .env.op -- ./scripts/put-github-token.sh
```

### 4. Basic 認証を設定する

```bash
./scripts/set-basic-auth.sh
```

パスワードを指定しなければランダムに生成し、SSM Parameter Store の `/github-traffic/basic-auth-password` に保存します。ユーザー名の既定は `admin` です。

```bash
aws ssm get-parameter --name /github-traffic/basic-auth-password --with-decryption --query Parameter.Value --output text
```

### 5. 初回の収集を実行する

スケジュールを待たずに一度実行すると、GitHub に残っている直近 14 日分が入ります。

```bash
aws lambda invoke --function-name "$(aws cloudformation describe-stacks --stack-name GithubTrafficStack \
  --query "Stacks[0].Outputs[?OutputKey=='CollectorFunctionName'].OutputValue" --output text)" /dev/stdout
```

出力の `DashboardUrl` を開くとダッシュボードが表示されます。

## 設定

`cdk.json` の `context` で変えられます。

| キー | 既定値 | 内容 |
|---|---|---|
| `githubTokenParameterName` | `/github-traffic/token` | トークンを置く SSM パラメータ名 |
| `scheduleExpression` | `cron(30 0 * * ? *)` | 収集の実行間隔（UTC）。GitHub の日付は UTC なので、UTC の日付が変わった直後に動かしています |

## 片付け

```bash
npx cdk destroy
```

収集した履歴を守るため、DynamoDB テーブルは `RETAIN` にしてあり、スタックを消しても残ります。不要ならコンソールから削除してください。SSM の 2 つのパラメータも手動で削除します。

## コスト

個人のリポジトリ数十件程度なら、DynamoDB・Lambda・S3・EventBridge Scheduler はほぼ無料枠に収まります。CloudFront も個人で見る程度のアクセスなら無料枠の範囲です。
