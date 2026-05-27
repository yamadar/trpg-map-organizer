# TRPG マップ自動タグ付け＆管理システム

ローカルにある TRPG 用マップ画像を **Google Gemini 2.5 Flash** で自動解析し、
地形・雰囲気・場所のタグを付けて SQLite に保存、**Streamlit** の WebUI から
タグ検索・閲覧できるツール。

## 主な機能
- 📂 指定フォルダを再帰的にスキャンし画像を発見
- 🤖 Gemini API による自動タグ付け (地形 / 雰囲気 / 場所 + 簡易説明)
- 💾 SQLite による永続化、増分解析（変更されたファイルのみ再解析）
- 🔎 タグ複数選択 (AND/OR) ＋ ファイル名検索
- 🖼️ グリッド表示＋プレビュー (全タグ・説明・パスを表示)

## 必要環境
- Python 3.10 以上
- Google AI Studio の API キー (無料枠あり)
  - 取得: <https://aistudio.google.com/apikey>

## セットアップ

```bash
# 1. リポジトリに入って仮想環境を作る
cd trpg-map-organizer
python3 -m venv .venv
source .venv/bin/activate

# 2. 依存をインストール
pip install -r requirements.txt

# 3. 設定ファイルを作成
cp .env.example .env                       # GEMINI_API_KEY を編集
cp config.example.yaml config.yaml          # target_folder を編集
```

`.env`:
```env
GEMINI_API_KEY=AIzaSy...
```

`config.yaml` の主な項目:
```yaml
target_folder: "~/Pictures/TRPG_Maps"   # 解析するフォルダ
database_path: "./data/maps.db"          # DB の保存先
gemini_model: "gemini-2.5-flash"         # 使用モデル
```

## 使い方

### 1. 解析を実行（DB を構築）
```bash
# 増分解析（新規・変更ファイルのみ）
python -m scripts.build_db

# 全件再解析
python -m scripts.build_db --rebuild

# テスト用に最初の N 件だけ
python -m scripts.build_db --limit 5

# API を呼ばず対象一覧だけ確認
python -m scripts.build_db --dry-run

# 詳細ログ
python -m scripts.build_db -v
```

### 2. WebUI を起動
```bash
streamlit run src/app.py
```

ブラウザで <http://localhost:8501> を開くと:
- サイドバーで地形・雰囲気・場所タグを複数選択して絞り込み
- AND/OR を切り替えて検索条件を変更
- ファイル名で部分一致検索
- カードの「詳細を見る」でフル解像度プレビュー＋全タグ表示

## タグ表記揺れの正規化

AI が付ける日本語タグは「大木 / 樹木 / 木」「河川 / 川」「のどかな / のどか」など
表記揺れが発生する。これを `tag_aliases.yaml` で一元管理する。

### 辞書フォーマット
```yaml
aliases:
  大木: 木        # 「大木」を見つけたら「木」に置き換える
  樹木: 木
  河川: 川
  のどかな: のどか
  中世風: 中世
```
チェーン可 (`大樹木: 樹木` と `樹木: 木` を書けば「大樹木」も「木」に集約)。

### 通常運用ワークフロー
```bash
# 1. 現状の全タグから Gemini に統合候補を生成させる
python -m scripts.suggest_aliases
#    → tag_aliases_suggested.yaml が生成される (要レビュー)

# 2. 内容を確認のうえ、採用する行を tag_aliases.yaml にコピー

# 3. 影響を dry-run で確認
python -m scripts.normalize_db --dry-run

# 4. DB に適用 (API 呼び出しなし、タグ書き換えのみ)
python -m scripts.normalize_db

# 5. Streamlit を再読み込みすると、すっきりしたタグで検索できる
```

build_db 実行時にも自動で正規化が適用される。

## プロジェクト構成

```
trpg-map-organizer/
├── .env.example              # 環境変数テンプレート
├── config.example.yaml       # 設定テンプレート
├── tag_aliases.yaml          # タグ正規化辞書 (git 管理の正本)
├── requirements.txt          # 依存パッケージ
├── README.md
├── src/
│   ├── config.py             # 設定ローダ
│   ├── db.py                 # SQLite 操作
│   ├── scanner.py            # ファイル走査・変更検出
│   ├── analyzer.py           # Gemini API クライアント
│   ├── normalize.py          # タグ正規化
│   └── app.py                # Streamlit UI
├── scripts/
│   ├── build_db.py           # DB 構築 CLI
│   ├── normalize_db.py       # 既存 DB に正規化を適用
│   └── suggest_aliases.py    # Gemini に統合候補を提案させる
└── data/                     # DB ファイル (gitignore)
```

## データベース

`maps` テーブル:

| カラム | 型 | 説明 |
|---|---|---|
| id | INTEGER PK | 自動採番 |
| file_path | TEXT UNIQUE | ファイルの絶対パス |
| file_name | TEXT | ファイル名 |
| file_size | INTEGER | バイト数（変更検出用） |
| file_mtime | REAL | 更新時刻（変更検出用） |
| file_hash | TEXT | 先頭 64KB の SHA1 |
| terrain_tags | TEXT (JSON) | 地形タグ配列 |
| mood_tags | TEXT (JSON) | 雰囲気タグ配列 |
| location_tags | TEXT (JSON) | 場所タグ配列 |
| description | TEXT | AI 生成の説明文 |
| analyzed_at | TIMESTAMP | 最終解析日時 |
| created_at | TIMESTAMP | 初回登録日時 |
| updated_at | TIMESTAMP | 更新日時 |

タグは JSON 配列として保存し、検索時は SQLite の JSON1 拡張で展開して照合する。

## トラブルシュート

- **`GEMINI_API_KEY が未設定です`**: `.env` を作成し API キーを設定する。
- **`ターゲットフォルダが存在しません`**: `config.yaml` の `target_folder` を確認する。
- **解析が遅い / 429 エラー**: `config.yaml` の `api_min_interval_sec` を増やす。
- **画像が表示されない**: ファイルが移動・削除されていないか確認。プレビューに警告が出る。

## セキュリティ
- 画像データはローカルから移動せず、解析時のみ Gemini API に送信する。
- API キーは `.env` に保存し、`.gitignore` で除外済み。
- 生成された DB (`./data/maps.db`) も `.gitignore` で除外済み。

## ライセンス
ローカル利用を想定した個人プロジェクト。
