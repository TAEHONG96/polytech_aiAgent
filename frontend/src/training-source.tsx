import { useEffect, useState } from 'react'
import { api, errorMessage, statuses } from './api'
import type { Status } from './api'
import { useCompanionPart } from './companion/companion-context'

const runPath = /^\/simulation-runs\/[\da-f-]{36}/i
const sourcePath = /^\/(sops\/[\da-f-]{36}|tickets\/[\da-f-]{36}\/incident-sources\/[\da-f-]{36}|outgoing-handover\/revisions\/\d+)$/i

export function isTrainingSource(path: string) {
  const base = runPath.exec(path)?.[0]
  return !!base && sourcePath.test(path.slice(base.length))
}

export function trainingSourceHref(url: string) {
  const path = url.replace(/^\/api\/v1(?=\/)/, '')
  if (isTrainingSource(path)) return path
  if (/^\/simulation-runs\/[\da-f-]{36}\/tickets\/[\da-f-]{36}(\/operations)?$/i.test(path)) {
    return path.replace(/\/operations$/, '#work')
  }
  return url
}

type Source = {
  title?: string
  occurred_at?: string
  resolution?: string
  error_code?: string | null
  ticket_key?: string | null
  service?: string | null
  asset_kind?: string | null
  host?: { hostname: string; host_key: string } | null
  extracted_text?: string | null
  content_type?: string
  original_url?: string
  revision?: number
  content?: { summary: string | null; open_items: { ticket_id: string; next_action: string | null }[] }
  items?: { id: string; title: string; status: Status; next_action: string | null }[]
}

function sopInline(text: string) {
  return text.split(/(`[^`]+`)/).map((part, index) => part.startsWith('`') && part.endsWith('`')
    ? <code key={index}>{part.slice(1, -1)}</code> : part)
}

function SopMarkdown({ text, title }: { text: string; title?: string }) {
  const blocks = text.trim().replace(/\r\n/g, '\n').replace(/^(#{1,6} .+)\n(?!\n)/gm, '$1\n\n').split(/\n\s*\n/)
  return <div className="training-sop">{blocks.map((block, index) => {
    const heading = /^(#{1,6}) (.+)$/.exec(block)
    if (heading) {
      if (index === 0 && heading[1] === '#' && title?.replace(/^(?:교육용|합성) SOP · /, '') === heading[2]) return null
      const level = Math.max(2, heading[1].length)
      const Heading = `h${level}` as 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
      return <Heading key={index}>{sopInline(heading[2])}</Heading>
    }
    const lines = block.split('\n')
    const ordered = /^\d+\. +/
    const unordered = /^[-*] +/
    const marker = lines.every(line => ordered.test(line)) ? ordered : lines.every(line => unordered.test(line)) ? unordered : null
    if (marker) {
      const List = marker === ordered ? 'ol' : 'ul'
      return <List key={index}>{lines.map((line, item) => <li key={item}>{sopInline(line.replace(marker, ''))}</li>)}</List>
    }
    return <p className="knowledge-text" key={index}>{sopInline(block)}</p>
  })}</div>
}

export function TrainingSource({ path }: { path: string }) {
  const base = runPath.exec(path)![0]
  const [source, setSource] = useState<Source>()
  const [error, setError] = useState('')
  const incident = path.includes('/incident-sources/')
  const section = incident ? '호스트 과거 장애 · 읽기 전용' : path.includes('/sops/') ? '승인 SOP' : '확정 인계 리비전'
  const sopId = source && path.includes('/sops/') ? path.split('/').at(-1) : undefined
  useCompanionPart('source', { phase: error ? 'error' : source ? 'ready' : 'loading' })
  useEffect(() => {
    const controller = new AbortController()
    api<Source>(path, { signal: controller.signal }).then(result => { if (!controller.signal.aborted) setSource(result) })
      .catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)) })
    return () => controller.abort()
  }, [path])
  return <>
    <nav className="training-nav" aria-label="근거 탐색"><a href={base}>근무·티켓 목록</a>
      {source?.original_url && <a href={base + '/sops'}>SOP 목록</a>}
      {incident && <a href={path.split('/incident-sources/')[0] + '#incidents'}>현재 티켓의 과거 장애 목록</a>}
      <a href={base + '#evaluation'}>실습 결과</a></nav>
    <article className="surface" data-companion-destination={sopId ? 'sop' : undefined} data-companion-source-id={sopId}>
      <p className="training-context">{section}</p>
      <h1>{source?.title ?? (source?.revision ? `인계 리비전 ${source.revision}` : '실습 근거')}</h1>
      {error && <p className="notice" role="alert">{error}</p>}
      {!source && !error && <p role="status">근거를 확인하는 중입니다.</p>}
      {incident && source && <>
        <h2>장애 개요</h2>
        <dl className="details">
          <div><dt>티켓 키</dt><dd><code>{source.ticket_key ?? '미등록'}</code></dd></div>
          <div><dt>서비스</dt><dd>{source.service ?? '미등록'}</dd></div>
          <div><dt>호스트명</dt><dd>{source.host?.hostname ?? '미등록'}</dd></div>
          <div><dt>호스트 키</dt><dd>{source.host?.host_key ?? '미등록'}</dd></div>
          <div><dt>장애 분류</dt><dd>{source.asset_kind === 'memory' ? '메모리' : source.asset_kind === 'storage' ? '스토리지' : (source.asset_kind ?? '미등록')}</dd></div>
          <div><dt>장애 코드</dt><dd><code>{source.error_code ?? '미등록'}</code></dd></div>
          <div><dt>발생 시각</dt><dd>{source.occurred_at
            ? new Date(source.occurred_at).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' }) + ' KST' : '미등록'}</dd></div>
        </dl>
        <h2>당시 조치 결과</h2><p className="knowledge-text">{source.resolution || '기록 없음'}</p>
      </>}
      {source?.original_url && <p><a href={source.original_url}>SOP 원본 열기</a></p>}
      {source?.extracted_text && (source.content_type === 'text/markdown'
        ? <SopMarkdown text={source.extracted_text} title={source.title} /> : <div className="knowledge-text">{source.extracted_text}</div>)}
      {source?.content && <>
        <h2>인계 요약</h2><p className="knowledge-text">{source.content.summary ?? '기록된 요약이 없습니다.'}</p>
        <h2>포함된 티켓</h2>
        {source.items?.length ? <ul className="training-list">{source.items.map(item => <li key={item.id}>
          <div><a href={`${base}/tickets/${item.id}`}>{item.title}</a><p>{source.content?.open_items.find(note => note.ticket_id === item.id)?.next_action
            ?? item.next_action ?? '기록된 다음 조치가 없습니다.'}</p></div><span className="training-badge">{statuses[item.status]}</span></li>)}</ul>
          : <p>포함된 티켓이 없습니다.</p>}
      </>}
    </article>
  </>
}
