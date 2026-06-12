"""Academic literature search, storage, and PDF download tools."""

from pathlib import Path

from aero.toolbox.download_progress import download_progress_reporter, format_size
from aero.toolbox.paths import find_project_dir, short_path
from aero.toolbox.registry import register_tool


@register_tool(
    name="search_literature",
    description=(
        "在学术文献库中搜索论文，支持 OpenAlex（正式出版物）和 arXiv（预印本）两种数据源。"
        "通过 source 参数切换：默认 'openalex'，选 'arxiv' 搜索预印本论文。"
        "返回标题、作者、摘要、引用数、Open Access 状态等元信息。"
        "这是预览搜索，不会保存任何文件。如需保存结果，请使用 save_literature。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": (
                    "搜索关键词，按标题/摘要/全文匹配，如 tropical cyclone、极端降水、ENSO"
                ),
            },
            "author": {
                "type": "string",
                "description": "作者名（部分匹配），如 Manabe, S.、Bjerknes, J.、王斌",
            },
            "journal": {
                "type": "string",
                "description": (
                    "期刊/来源名（部分匹配），如 Nature、Journal of Climate、气象学报。"
                    "arxiv 模式下此参数无效"
                ),
            },
            "year_from": {
                "type": "integer",
                "description": "发表年份下限",
            },
            "year_to": {
                "type": "integer",
                "description": "发表年份上限",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少篇，默认 10",
            },
            "source": {
                "type": "string",
                "description": (
                    "文献数据源：'openalex'（默认）搜索正式出版物，"
                    "'arxiv' 搜索预印本论文（时效性更强，适合最新研究）。"
                ),
                "enum": ["openalex", "arxiv"],
            },
        },
    },
)
async def search_literature(
    keyword: str | None = None,
    author: str | None = None,
    journal: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 10,
    source: str = "openalex",
) -> dict:
    """Search scholarly works by keyword, author, journal."""
    from aero.data.literature import search_works

    if not keyword and not author and not journal:
        return {
            "found": False,
            "error": "keyword、author、journal 至少填一个",
        }

    result = await search_works(
        keyword=keyword,
        author=author,
        journal=journal,
        year_from=year_from,
        year_to=year_to,
        limit=limit,
        source=source,
    )

    if not result["found"]:
        msg = "未找到匹配的文献"
        parts = []
        if keyword:
            parts.append(f"keyword=「{keyword}」")
        if author:
            parts.append(f"author=「{author}」")
        if journal:
            parts.append(f"journal=「{journal}」")
        if parts:
            msg += f"（{', '.join(parts)}）"
        if source == "arxiv":
            return {
                "found": False,
                "keyword": keyword or "",
                "author": author or "",
                "journal": journal or "",
                "source": "arxiv",
                "message": msg,
                "suggestions": [
                    "尝试缩短关键词或使用更通用的术语",
                    "确认作者名拼写是否正确",
                    "arXiv 预印本可能还未收录该研究方向的早期工作",
                ],
            }
        return {
            "found": False,
            "keyword": keyword or "",
            "author": author or "",
            "journal": journal or "",
            "message": msg,
            "suggestions": [
                "尝试缩短关键词或使用更通用的术语",
                "确认作者名拼写是否正确",
                "放宽年份范围或去掉期刊限制",
            ],
        }

    return result


@register_tool(
    name="save_literature",
    description=(
        "将指定论文的完整元信息（标题、作者、摘要、DOI 等）保存到本地 literature/ 目录。"
        "默认只保存元信息；传 download_pdf=true 才会尝试下载全文 PDF。"
        "work_id 来自 search_literature 返回的 work_id 字段。"
        "全文下载会自动查询多个合法开放来源，并在某个来源受限时继续尝试其他来源。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "work_id": {
                "type": "string",
                "description": (
                    "work ID，如 W4240686422（OpenAlex）或 arxiv:2301.12345（arXiv），"
                    "来自 search_literature 返回结果"
                ),
            },
            "download_pdf": {
                "type": "boolean",
                "description": (
                    "是否同时尝试下载全文 PDF，默认 false。"
                    "arXiv 论文走直链下载，正式出版物从 OA 源查找。"
                ),
            },
        },
        "required": ["work_id"],
    },
)
async def save_literature(work_id: str, download_pdf: bool = False) -> dict:
    """Save literature metadata and optionally download full-text PDF."""
    from aero.agent.progress import emit_progress
    from aero.data.literature import get_work_detail, save_metadata, update_index

    is_arxiv = work_id.startswith("arxiv:")
    if is_arxiv:
        from aero.data.literature_pdf import download_pdf as _download_pdf
    else:
        from aero.data.literature_pdf import download_pdf_candidates, resolve_pdf_urls

    project_dir = find_project_dir()
    literature_dir = project_dir / "literature"
    literature_dir.mkdir(parents=True, exist_ok=True)

    emit_progress("正在获取论文元信息")
    work = await get_work_detail(work_id)
    if work is None:
        return {
            "success": False,
            "error": f"无法获取 work_id={work_id} 的文献信息，请确认 ID 是否正确",
        }

    emit_progress("正在写入本地文献库")
    paper_dir = save_metadata(work, literature_dir)
    update_index(work, literature_dir)

    result: dict = {
        "success": True,
        "work_id": work_id,
        "title": work["title"],
        "authors": work["authors"],
        "year": work["publication_year"],
        "doi": work["doi"],
        "is_oa": work["is_oa"],
        "oa_status": work["oa_status"],
        "saved_to": short_path(paper_dir),
    }

    if download_pdf:
        if is_arxiv:
            emit_progress("正在从 arXiv 下载全文 PDF")
            pdf_url = work.get("pdf_url", "")
            if not pdf_url:
                arxiv_id = work_id.replace("arxiv:", "")
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            pdf_path = paper_dir / "paper.pdf"
            dl_result = await _download_pdf(pdf_url, pdf_path)
            result["pdf_download"] = dl_result
            result["pdf_sources"] = ["arxiv"]
        else:
            emit_progress("正在从多个开放来源查找全文 PDF")
            pdf_info = await resolve_pdf_urls(work)
            if pdf_info["candidates"]:
                emit_progress(f"已找到 {len(pdf_info['candidates'])} 个候选来源，开始依次尝试")
                pdf_path = paper_dir / "paper.pdf"
                dl_result = await download_pdf_candidates(
                    pdf_info["candidates"],
                    pdf_path,
                    on_progress=download_progress_reporter(),
                )
                result["pdf_download"] = dl_result
                result["pdf_sources"] = pdf_info["tried_sources"]
            else:
                emit_progress("未找到可直接下载的开放获取 PDF")
                result["pdf_download"] = {
                    "success": False,
                    "error": "全文 PDF 不可用",
                    "tried_sources": pdf_info["tried_sources"],
                    "suggestion": (
                        "该论文未在 OpenAlex、Europe PMC、Unpaywall、"
                        "Semantic Scholar 或 Crossref 中找到可下载全文。"
                        "你可以通过 DOI 链接到期刊主页查看，或通过学校/机构图书馆访问。"
                    ),
                }

    return result


@register_tool(
    name="download_literature_pdf",
    description=(
        "对已保存到 literature/ 的论文，尝试下载全文 PDF。"
        "通过 work_id 或 doi 指定目标论文。"
        "工具会从多个合法开放来源发现全文，并在 403、登录页或失效链接后自动尝试下一来源。"
        "不会绕过付费墙、登录或反爬验证。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "work_id": {
                "type": "string",
                "description": "work ID，如 W4240686422（OpenAlex）或 arxiv:2301.12345（arXiv）",
            },
            "doi": {
                "type": "string",
                "description": "DOI 号，如 10.1038/nclimate1410",
            },
        },
    },
)
async def download_literature_pdf(
    work_id: str | None = None,
    doi: str | None = None,
) -> dict:
    """Download full-text PDF for a previously saved literature entry."""
    from aero.data.literature import load_saved_metadata, resolve_doi
    from aero.data.literature_pdf import download_pdf as _download_pdf

    project_dir = find_project_dir()
    literature_dir = project_dir / "literature"

    if not work_id and not doi:
        return {"success": False, "error": "work_id 和 doi 至少填一个"}

    is_arxiv = bool(work_id and work_id.startswith("arxiv:"))
    if is_arxiv:
        from aero.data.literature_arxiv import get_arxiv_detail

    if not is_arxiv:
        from aero.data.literature_pdf import download_pdf_candidates, resolve_pdf_urls

    work = None
    paper_dir = None
    if work_id:
        work = load_saved_metadata(work_id, literature_dir)
        if work:
            paper_dir = Path(work.pop("_dir", "")) if work.get("_dir") else None
    if not work and work_id and is_arxiv:
        work = await get_arxiv_detail(work_id)
    if not work and doi:
        work = await resolve_doi(doi)

    if not work:
        return {
            "success": False,
            "error": (
                "未找到该论文的元信息。如果已调用 save_literature 保存过，"
                "请确认 work_id 是否正确；否则请先调用 save_literature。"
            ),
        }

    if paper_dir is None:
        from aero.data.literature import save_metadata

        paper_dir = save_metadata(work, literature_dir)

    if is_arxiv:
        pdf_url = work.get("pdf_url", "")
        if not pdf_url:
            arxiv_id = work_id.replace("arxiv:", "") if work_id else ""
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        pdf_path = paper_dir / "paper.pdf"
        dl_result = await _download_pdf(pdf_url, pdf_path)
        return {
            "success": dl_result["success"],
            "work_id": work["work_id"],
            "title": work["title"],
            "pdf_download": dl_result,
            "tried_sources": ["arxiv"],
        }

    pdf_info = await resolve_pdf_urls(work)
    if not pdf_info["candidates"]:
        return {
            "success": False,
            "error": "全文 PDF 不可用",
            "tried_sources": pdf_info["tried_sources"],
            "suggestion": "所有合法开放来源均未提供 PDF，可能需要通过 DOI 使用机构访问权限。",
        }

    pdf_path = paper_dir / "paper.pdf"
    dl_result = await download_pdf_candidates(
        pdf_info["candidates"],
        pdf_path,
        on_progress=download_progress_reporter(),
    )

    return {
        "success": dl_result["success"],
        "work_id": work["work_id"],
        "title": work["title"],
        "pdf_download": dl_result,
        "tried_sources": pdf_info["tried_sources"],
        "candidate_count": len(pdf_info["candidates"]),
    }


@register_tool(
    name="list_literature",
    description=(
        "列出本地 literature/ 目录中已保存的所有论文，包括标题、作者、年份、是否已下载 PDF。"
        "类似 list_figures，用于查看已有文献库。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
async def list_literature() -> dict:
    """List all locally saved literature entries."""
    from aero.data.literature import list_saved

    project_dir = find_project_dir()
    literature_dir = project_dir / "literature"
    literature_dir.mkdir(parents=True, exist_ok=True)

    entries = list_saved(literature_dir)

    if not entries:
        return {
            "status": "success",
            "directory": short_path(literature_dir),
            "relative_directory": "literature",
            "entry_count": 0,
            "message": "literature/ 目录中暂无已保存的论文",
            "entries": [],
        }

    files = []
    for e in entries:
        item = {
            "work_id": e["work_id"],
            "title": e["title"],
            "authors": e["authors"][:5],
            "year": e["year"],
            "source": e["source"],
            "doi": e["doi"],
            "has_pdf": e["has_pdf"],
            "dir": e["dir"],
        }
        if e["has_pdf"] and e["pdf_size"] is not None:
            item["pdf_size"] = e["pdf_size"]
            item["pdf_size_human"] = format_size(e["pdf_size"])
        files.append(item)

    return {
        "status": "success",
        "directory": short_path(literature_dir),
        "relative_directory": "literature",
        "entry_count": len(files),
        "entries": files,
    }
