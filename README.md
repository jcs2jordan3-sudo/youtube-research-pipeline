# YouTube Research Pipeline

YouTube 영상 URL 목록을 입력하면 메타데이터/자막 수집 → 영상별 Markdown 문서화 → NotebookLM용 통합 브리프 → Notion 업로드용 JSON → Obsidian 노트까지 자동으로 생성하는 로컬 리서치 자동화 도구입니다.

## 주요 기능

- **메타데이터 수집**: yt-dlp Python API로 영상 정보를 다운로드 없이 추출
- **자막 추출**: youtube-transcript-api (1차) → yt-dlp (fallback) 이중 구조
- **Markdown 문서 생성**: 영상별 구조화된 리서치 노트 자동 생성
- **NotebookLM 브리프**: 여러 영상을 묶어 교차 분석용 통합 문서 생성
- **Notion 페이로드 + API 업로드**: JSON payload 생성 및 실제 API 업로드
- **Obsidian 연동**: YAML frontmatter + wikilink 형태 노트 자동 생성
- **LLM 요약**: Claude API로 placeholder 섹션 자동 채움 (선택)
- **병렬 처리**: ThreadPoolExecutor 기반 동시 처리
- **중복 체크**: 이미 처리된 영상 자동 스킵

## 설치

### 요구사항

- Python 3.10+
- pip

### 설치 방법

```bash
cd youtube_research_pipeline
pip install -r requirements.txt
```

### 선택 의존성

```bash
# LLM 요약 기능 사용 시
pip install anthropic>=0.40.0
```

### 환경 설정

```bash
cp .env.example .env
# .env 파일을 편집하여 필요한 값 설정
```

## 사용법

### 1. URL 입력

`data/inbox/youtube_urls.txt`에 YouTube URL을 한 줄씩 입력합니다:

```
https://www.youtube.com/watch?v=VIDEO_ID_1
https://www.youtube.com/watch?v=VIDEO_ID_2
https://youtu.be/VIDEO_ID_3
```

`#`으로 시작하는 줄은 주석으로 무시됩니다.

### 2. 실행

```bash
# 기본 실행
python -m app.main

# 병렬 처리 + 중복 스킵
python -m app.main --parallel 3 --skip-existing

# 풀 옵션
python -m app.main \
  --parallel 3 \
  --skip-existing \
  --obsidian \
  --summarize \
  --theme "AI 자동화 리서치" \
  -u my_urls.txt
```

### CLI 옵션

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `--urls`, `-u` | URL 파일 경로 | `data/inbox/youtube_urls.txt` |
| `--theme`, `-t` | 리서치 테마명 | `"YouTube Research"` |
| `--lang` | 자막 언어 우선순위 (쉼표 구분) | `"ko,en"` |
| `--output-dir`, `-o` | 출력 디렉토리 | 기본 `data/` |
| `--skip-existing` | 이미 처리된 영상 스킵 | off |
| `--parallel`, `-p` | 병렬 워커 수 | `1` (순차) |
| `--summarize` | Claude API로 LLM 요약 생성 | off |
| `--obsidian` | Obsidian 노트 내보내기 | off |
| `--notion-upload` | Notion API 직접 업로드 | off |

### 3. 실행 결과 예시

```
============================================================
 AI 자동화 리서치
 2024-01-15 14:30:00 | 3 URLs | workers=3
 --skip-existing enabled
 --obsidian enabled
============================================================

Processing 3 URLs with 3 parallel workers...

[1/3] https://www.youtube.com/watch?v=...
  -> OK
[2/3] https://www.youtube.com/watch?v=...
  -> OK (transcript: not_available)
[3/3] https://www.youtube.com/watch?v=...
  -> SKIPPED (already processed)

Research brief: data/notebooklm/research_brief.md
Notion payload: data/notion/notion_payload.json

Exporting Obsidian notes...
  Obsidian note: data/obsidian/Video Title.md
  Obsidian index: data/obsidian/_YouTube Research Index.md

============================================================
 Pipeline Complete
============================================================
  Total:      3
  Skipped:    1
  Processed:  2
  Success:    2
  Failed:     0
  Transcript: 1/2
  Manifest:   data/manifest.json
```

## 출력 파일

| 경로 | 설명 |
|------|------|
| `data/raw/{video_id}.info.json` | yt-dlp 원본 메타데이터 |
| `data/raw/{video_id}.transcript.json` | 자막 추출 결과 |
| `data/processed/{video_id}.md` | 영상별 Markdown 리서치 노트 |
| `data/notebooklm/research_brief.md` | NotebookLM용 통합 리서치 브리프 |
| `data/notion/notion_payload.json` | Notion API 업로드용 JSON |
| `data/obsidian/{title}.md` | Obsidian YAML frontmatter 노트 |
| `data/obsidian/_YouTube Research Index.md` | Obsidian 인덱스 노트 |
| `data/manifest.json` | 파이프라인 실행 결과 매니페스트 |
| `data/failed_urls.txt` | 실패한 URL 목록 |
| `logs/pipeline.log` | 상세 실행 로그 |

## NotebookLM 활용

1. `data/notebooklm/research_brief.md`를 NotebookLM에 소스로 업로드
2. 개별 영상 문서 `data/processed/*.md`도 추가 소스로 업로드 가능
3. 브리프에 포함된 "Recommended Follow-up Questions"를 NotebookLM에서 질문으로 활용

## Notion 적재

### 방법 1: JSON payload 수동 활용
`data/notion/notion_payload.json`의 내용을 참고하여 Notion에 수동 입력

### 방법 2: API 직접 업로드
1. `.env`에 설정:
   ```
   NOTION_API_KEY=secret_xxxxx
   NOTION_DATABASE_ID=xxxxx
   ```
2. Notion DB에 다음 속성 생성:
   - Title (title), Source URL (url), Channel (rich_text)
   - Published Date (date), Topic Tags (multi_select)
   - Status (select), NotebookLM Ready (checkbox)
   - Confidence (select), Collected At (rich_text)
3. 실행: `python -m app.main --notion-upload`

## Obsidian 연동

`--obsidian` 플래그를 사용하면 `data/obsidian/`에 노트가 생성됩니다:
- YAML frontmatter (title, url, channel, tags 등)
- `[[wikilink]]` 형태의 채널/태그 링크
- 채널별 그룹핑된 인덱스 노트
- Obsidian vault 디렉토리에 복사하여 바로 사용 가능

## LLM 요약

`--summarize` 플래그를 사용하면 Claude API가 Markdown의 placeholder 섹션을 자동으로 채웁니다:
- 한줄 요약, 핵심 주장 5개, 핵심 포인트
- 실무 적용 포인트, 리스크/한계/검증 필요

필요: `pip install anthropic` + `.env`에 `ANTHROPIC_API_KEY` 설정

## 오류 점검

| 증상 | 원인 / 해결 |
|------|-------------|
| `No URLs found` | `data/inbox/youtube_urls.txt`에 URL이 없음 |
| `Could not extract video ID` | URL 형식이 올바르지 않음 |
| `yt-dlp DownloadError` | 영상이 비공개/삭제/지역제한일 수 있음 |
| `TranscriptsDisabled` | 영상 소유자가 자막을 비활성화함 |
| `ModuleNotFoundError: yt_dlp` | `pip install yt-dlp` |
| `ModuleNotFoundError: anthropic` | `pip install anthropic` (--summarize 사용 시) |
| 인코딩 오류 (Windows) | 터미널: `chcp 65001` |
| Notion 429 에러 | 자동 재시도됨 (최대 3회, 백오프 적용) |

## 프로젝트 구조

```
youtube_research_pipeline/
  app/
    __init__.py
    __main__.py          # python -m app 진입점
    main.py              # 파이프라인 오케스트레이터 + CLI
    scraper.py           # yt-dlp 메타데이터 수집
    transcript.py        # 자막/전사 텍스트 추출
    formatter.py         # Markdown 문서 생성
    brief_generator.py   # NotebookLM 통합 브리프
    notion_export.py     # Notion JSON payload + API 업로드
    obsidian_export.py   # Obsidian vault 노트 내보내기
    summarizer.py        # Claude API LLM 요약
    models.py            # 데이터 모델
    utils.py             # 유틸리티 함수
  data/
    inbox/               # URL 입력
    raw/                 # 원본 메타데이터/자막
    processed/           # Markdown 문서
    notebooklm/          # 리서치 브리프
    notion/              # Notion payload
    obsidian/            # Obsidian 노트
  logs/                  # 실행 로그
  requirements.txt
  .env.example
  README.md
```
