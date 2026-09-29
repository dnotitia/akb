import { createContext, useContext, useLayoutEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
export type MarkdownLocale = 'en' | 'ko'

export type MarkdownUploadPlacementError =
  | 'invalidAssetTarget'
  | 'originalPositionUnavailable'
  | 'imageReplacementFailed'
  | 'imageInsertionFailed'

const en = {
  toolbar: {
    ariaLabel: 'Text formatting',
    blockType: 'Block type',
    marks: 'Marks',
    lists: 'Lists',
    blocks: 'Blocks',
    insert: 'Insert',
    history: 'History',
    paragraph: 'Paragraph',
    heading1: 'Heading 1',
    heading2: 'Heading 2',
    heading3: 'Heading 3',
    bold: 'Bold',
    italic: 'Italic',
    strikethrough: 'Strikethrough',
    inlineCode: 'Inline code',
    bulletList: 'Bulleted list',
    numberedList: 'Numbered list',
    taskList: 'Task list',
    blockquote: 'Blockquote',
    codeBlock: 'Code block',
    horizontalRule: 'Horizontal rule',
    insertLink: 'Insert link',
    editLink: 'Edit link',
    insertTable: 'Insert table',
    undo: 'Undo',
    redo: 'Redo',
  },
  editing: {
    modeGroup: 'Editor mode',
    wysiwyg: 'WYSIWYG',
    source: 'Source',
    sourceField: 'Markdown source',
    sourcePlaceholder: 'Write Markdown source…',
  },
  link: {
    insertButton: 'Insert link',
    editButton: 'Edit link',
    saveButton: 'Save link',
    insertTitle: 'Insert link',
    editTitle: 'Edit link',
    description: 'Add a safe destination and choose the text readers will see.',
    url: 'URL',
    text: 'Text',
    textPlaceholder: 'Link text',
    textHint: 'Leave blank to use the destination as the visible text.',
    cancel: 'Cancel',
    remove: 'Remove link',
    close: 'Close dialog',
    invalidUrl: 'Enter an http(s), email, phone, anchor, or relative URL.',
  },
  linkSearch: {
    inputLabel: 'Search resources',
    inputPlaceholder: 'Find a document or file',
    searching: 'Searching…',
    empty: 'No matching documents or files found.',
    error: 'Unable to search resources. Check your access and try again.',
    retry: 'Retry search',
    results: 'Resource search results',
    document: 'Document',
    file: 'File',
    resource: 'Resource',
    resultName: (title: string, kind: string) => `${title} (${kind})`,
  },
  table: {
    editableTable: 'Editable table',
    readOnlyTable: 'Table',
    actions: 'Table actions',
    insertTable: 'Insert table',
    addRow: 'Add row after selected row',
    addColumn: 'Add column right of selected column',
    removeRow: 'Remove selected row',
    removeColumn: 'Remove selected column',
    continueBelow: 'Continue below',
    deleteTable: 'Delete table',
    scrollable: 'Scrollable table',
  },
  image: {
    loading: (alt: string) => (alt ? `Loading image: ${alt}` : 'Loading image'),
    unavailable: (alt: string) => (alt ? `Image unavailable: ${alt}` : 'Image unavailable'),
  },
  imageMenu: {
    editDescription: (alt: string) => alt ? `Edit image description: ${alt}` : 'Edit image description',
    replaceImage: (alt: string) => alt ? `Replace image: ${alt}` : 'Replace image',
    removeImage: (alt: string) => alt ? `Remove image: ${alt}` : 'Remove image',
    dialogTitle: 'Image description',
    dialogDescription: 'This text is used as the image alt text and visible caption.',
    description: 'Description',
    cancel: 'Cancel',
    saveDescription: 'Save description',
    descriptionRequired: 'Describe the image so it remains understandable without sight.',
    closeDialog: 'Close dialog',
  },
  imageUpload: {
    group: 'Attachments',
    insert: 'Insert image',
    uploading: 'Uploading image',
    checking: (name: string) => `Checking ${name}`,
    cancel: 'Cancel upload',
    failed: 'Image upload failed',
    queued: 'Images waiting to upload',
    retry: 'Retry',
    upload: 'Upload',
    chooseAnother: 'Choose another',
    dismiss: 'Dismiss',
    successful: 'Successful images remain in the draft.',
    cancelled: (count: number) => {
      const number = new Intl.NumberFormat('en').format(count)
      const image = new Intl.PluralRules('en').select(count) === 'one' ? 'image' : 'images'
      return `${number} ${image} cancelled.`
    },
    remaining: (count: number) => {
      const number = new Intl.NumberFormat('en').format(count)
      const image = new Intl.PluralRules('en').select(count) === 'one' ? 'image' : 'images'
      return `${number} ${image} remain in this batch.`
    },
    previousBatchFinished: 'The previous image batch finished. Upload the next batch when ready.',
    placementError: (reason: MarkdownUploadPlacementError) => {
      switch (reason) {
        case 'invalidAssetTarget': return 'The image upload returned an invalid asset target.'
        case 'originalPositionUnavailable': return 'The original image position is no longer available.'
        case 'imageReplacementFailed': return 'The image could not be replaced at its original position.'
        case 'imageInsertionFailed': return 'The image could not be inserted at its original position.'
      }
    },
  },
  slash: {
    header: 'Insert block',
    escapeHint: 'Esc',
    sections: {
      text: 'Text',
      lists: 'Lists',
      structure: 'Structure',
    },
    footer: {
      navigation: '↑↓ Navigate',
      insert: '↵ Insert',
      close: 'Esc Close',
    },
    empty: 'No matching blocks.',
    commands: {
      heading1: { label: 'Heading 1', description: 'Large section heading' },
      heading2: { label: 'Heading 2', description: 'Medium section heading' },
      heading3: { label: 'Heading 3', description: 'Small section heading' },
      quote: { label: 'Quote', description: 'Call out a quotation' },
      bulletList: { label: 'Bullet list', description: 'Create an unordered list' },
      numberedList: { label: 'Numbered list', description: 'Create an ordered list' },
      taskList: { label: 'Task list', description: 'Track work with checkboxes' },
      table: { label: 'Table', description: 'Insert a basic 3 × 2 table' },
      codeBlock: { label: 'Code block', description: 'Add a fenced code block' },
      divider: { label: 'Divider', description: 'Separate sections with a rule' },
    },
  },
  reference: {
    header: 'Insert reference',
    unavailable: 'Reference unavailable',
    resolving: 'Resolving reference',
    escapeHint: 'Esc',
    sections: {
      person: 'People',
      issue: 'Issues',
      document: 'Documents',
      file: 'Files',
    },
    searching: 'Searching…',
    empty: 'No matching references.',
    error: 'Unable to search references.',
    footer: {
      navigation: '↑↓ Navigate',
      insert: '↵ Insert',
      close: 'Esc Close',
    },
  },
  codeRegion: (language?: string) =>
    language ? `Scrollable ${language} code block` : 'Scrollable code block',
  taskCheckbox: (text: string) =>
    `Task item checkbox for ${text || 'empty task item'}`,
}

type WidenMessageTypes<Value> = Value extends (...args: infer Args) => unknown
  ? (...args: Args) => string
  : Value extends string
    ? string
    : Value extends object
      ? { [Key in keyof Value]: WidenMessageTypes<Value[Key]> }
      : Value

export type MarkdownMessages = WidenMessageTypes<typeof en>

const ko: MarkdownMessages = {
  toolbar: {
    ariaLabel: '서식 도구',
    blockType: '블록 유형',
    marks: '글자 서식',
    lists: '목록',
    blocks: '블록',
    insert: '삽입',
    history: '편집 이력',
    paragraph: '문단',
    heading1: '제목 1',
    heading2: '제목 2',
    heading3: '제목 3',
    bold: '굵게',
    italic: '기울임꼴',
    strikethrough: '취소선',
    inlineCode: '인라인 코드',
    bulletList: '글머리 기호 목록',
    numberedList: '번호 매기기 목록',
    taskList: '작업 목록',
    blockquote: '인용문',
    codeBlock: '코드 블록',
    horizontalRule: '구분선',
    insertLink: '링크 삽입',
    editLink: '링크 수정',
    insertTable: '표 삽입',
    undo: '실행 취소',
    redo: '다시 실행',
  },
  editing: {
    modeGroup: '편집기 모드',
    wysiwyg: '시각 편집',
    source: '소스',
    sourceField: 'Markdown 원문',
    sourcePlaceholder: 'Markdown 원문을 입력하세요…',
  },
  link: {
    insertButton: '링크 삽입',
    editButton: '링크 수정',
    saveButton: '링크 저장',
    insertTitle: '링크 삽입',
    editTitle: '링크 수정',
    description: '안전한 주소를 입력하고 독자에게 보여 줄 텍스트를 정하세요.',
    url: 'URL',
    text: '텍스트',
    textPlaceholder: '링크 텍스트',
    textHint: '비워 두면 주소가 표시 텍스트로 사용됩니다.',
    cancel: '취소',
    remove: '링크 삭제',
    close: '대화상자 닫기',
    invalidUrl: 'http(s), 이메일, 전화번호, 앵커 또는 상대 경로 주소를 입력하세요.',
  },
  linkSearch: {
    inputLabel: '리소스 검색',
    inputPlaceholder: '문서 또는 파일 찾기',
    searching: '검색 중…',
    empty: '일치하는 문서나 파일이 없습니다.',
    error: '리소스를 검색할 수 없습니다. 접근 권한을 확인하고 다시 시도하세요.',
    retry: '다시 검색',
    results: '리소스 검색 결과',
    document: '문서',
    file: '파일',
    resource: '리소스',
    resultName: (title: string, kind: string) => `${title} (${kind})`,
  },
  table: {
    editableTable: '편집 가능한 표',
    readOnlyTable: '표',
    actions: '표 작업',
    insertTable: '표 삽입',
    addRow: '선택한 행 아래에 행 추가',
    addColumn: '선택한 열 오른쪽에 열 추가',
    removeRow: '선택한 행 삭제',
    removeColumn: '선택한 열 삭제',
    continueBelow: '아래에서 이어 쓰기',
    deleteTable: '표 삭제',
    scrollable: '스크롤 가능한 표',
  },
  image: {
    loading: (alt: string) => (alt ? `이미지 불러오는 중: ${alt}` : '이미지 불러오는 중'),
    unavailable: (alt: string) => (alt ? `이미지를 사용할 수 없음: ${alt}` : '이미지를 사용할 수 없음'),
  },
  imageMenu: {
    editDescription: (alt: string) => alt ? `이미지 설명 수정: ${alt}` : '이미지 설명 수정',
    replaceImage: (alt: string) => alt ? `이미지 교체: ${alt}` : '이미지 교체',
    removeImage: (alt: string) => alt ? `이미지 삭제: ${alt}` : '이미지 삭제',
    dialogTitle: '이미지 설명',
    dialogDescription: '이 텍스트는 이미지 대체 텍스트와 화면에 보이는 설명으로 사용됩니다.',
    description: '설명',
    cancel: '취소',
    saveDescription: '설명 저장',
    descriptionRequired: '이미지를 볼 수 없는 독자도 이해할 수 있도록 설명을 입력하세요.',
    closeDialog: '대화상자 닫기',
  },
  imageUpload: {
    group: '첨부',
    insert: '이미지 삽입',
    uploading: '이미지 업로드 중',
    checking: (name: string) => `${name} 확인 중`,
    cancel: '업로드 취소',
    failed: '이미지 업로드 실패',
    queued: '업로드 대기 중인 이미지',
    retry: '다시 시도',
    upload: '업로드',
    chooseAnother: '다른 이미지 선택',
    dismiss: '닫기',
    successful: '업로드에 성공한 이미지는 초안에 남아 있습니다.',
    cancelled: (count: number) => `이미지 ${new Intl.NumberFormat('ko').format(count)}개 업로드를 취소했습니다.`,
    remaining: (count: number) => `이 묶음에 이미지 ${new Intl.NumberFormat('ko').format(count)}개가 남아 있습니다.`,
    previousBatchFinished: '앞서 시작한 이미지 묶음이 끝났습니다. 준비되면 다음 묶음을 업로드하세요.',
    placementError: (reason: MarkdownUploadPlacementError) => {
      switch (reason) {
        case 'invalidAssetTarget': return '이미지 업로드에서 올바르지 않은 파일 주소를 받았습니다.'
        case 'originalPositionUnavailable': return '원래 이미지 위치를 더 이상 찾을 수 없습니다.'
        case 'imageReplacementFailed': return '원래 위치의 이미지를 교체하지 못했습니다.'
        case 'imageInsertionFailed': return '원래 위치에 이미지를 삽입하지 못했습니다.'
      }
    },
  },
  slash: {
    header: '블록 삽입',
    escapeHint: 'Esc',
    sections: {
      text: '텍스트',
      lists: '목록',
      structure: '구조',
    },
    footer: {
      navigation: '↑↓ 이동',
      insert: '↵ 삽입',
      close: 'Esc 닫기',
    },
    empty: '일치하는 블록이 없습니다.',
    commands: {
      heading1: { label: '제목 1', description: '큰 제목을 만듭니다' },
      heading2: { label: '제목 2', description: '중간 제목을 만듭니다' },
      heading3: { label: '제목 3', description: '작은 제목을 만듭니다' },
      quote: { label: '인용문', description: '인용 내용을 강조합니다' },
      bulletList: { label: '글머리 기호 목록', description: '순서 없는 목록을 만듭니다' },
      numberedList: { label: '번호 매기기 목록', description: '순서 있는 목록을 만듭니다' },
      taskList: { label: '작업 목록', description: '체크박스로 작업을 관리합니다' },
      table: { label: '표', description: '3 × 2 기본 표를 삽입합니다' },
      codeBlock: { label: '코드 블록', description: '코드 블록을 추가합니다' },
      divider: { label: '구분선', description: '구역을 선으로 나눕니다' },
    },
  },
  reference: {
    header: '참조 삽입',
    unavailable: '참조를 사용할 수 없음',
    resolving: '참조 확인 중',
    escapeHint: 'Esc',
    sections: {
      person: '사람',
      issue: '이슈',
      document: '문서',
      file: '파일',
    },
    searching: '검색 중…',
    empty: '일치하는 참조가 없습니다.',
    error: '참조를 검색할 수 없습니다.',
    footer: {
      navigation: '↑↓ 이동',
      insert: '↵ 삽입',
      close: 'Esc 닫기',
    },
  },
  codeRegion: (language?: string) =>
    language ? `스크롤 가능한 ${language} 코드 블록` : '스크롤 가능한 코드 블록',
  taskCheckbox: (text: string) =>
    `작업 항목 선택란: ${text || '내용 없는 작업 항목'}`,
}

const catalogs: Record<MarkdownLocale, MarkdownMessages> = { en, ko }

export function getMarkdownMessages(locale: MarkdownLocale): MarkdownMessages {
  return catalogs[locale]
}

export class MarkdownLocaleSource {
  private locale: MarkdownLocale
  private readonly listeners = new Set<() => void>()

  constructor(locale: MarkdownLocale) {
    this.locale = locale
  }

  getLocale(): MarkdownLocale {
    return this.locale
  }

  setLocale(locale: MarkdownLocale): void {
    if (locale === this.locale) return
    this.locale = locale
    for (const listener of [...this.listeners]) listener()
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }
}

interface MarkdownLocaleContextValue {
  locale: MarkdownLocale
  source: MarkdownLocaleSource
}

const MarkdownLocaleContext = createContext<MarkdownLocaleContextValue | null>(null)

export function MarkdownLocaleProvider({
  locale,
  children,
}: {
  locale: MarkdownLocale
  children: ReactNode
}) {
  const [source] = useState(() => new MarkdownLocaleSource(locale))
  const value = useMemo(() => ({ locale, source }), [locale, source])

  useLayoutEffect(() => {
    source.setLocale(locale)
  }, [locale, source])

  return (
    <MarkdownLocaleContext.Provider value={value}>
      {children}
    </MarkdownLocaleContext.Provider>
  )
}

export function useMarkdownLocale(): MarkdownLocale {
  return useContext(MarkdownLocaleContext)?.locale ?? 'en'
}

export function useMarkdownLocaleSource(): MarkdownLocaleSource | undefined {
  return useContext(MarkdownLocaleContext)?.source
}

export function useMarkdownMessages(): MarkdownMessages {
  return getMarkdownMessages(useMarkdownLocale())
}
