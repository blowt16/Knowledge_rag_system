/**
 * 会话头像。
 *
 * 都用 CSS/SVG 画，不引入图片资源 —— 头像只需要区分「谁在说话」，
 * 加两个 PNG 只会多两份要跟着主题一起维护的资源。
 */

/** 用户头像：用户名首字。用 code point 取，中文名首字不会被切坏。 */
export function UserAvatar({ name, size = 30 }: { name?: string; size?: number }) {
  const initial = [...(name ?? '')][0]?.toUpperCase() ?? '?'
  return (
    <span className="avatar avatar-user" style={{ width: size, height: size, fontSize: size * 0.44 }}
          aria-hidden="true">
      {initial}
    </span>
  )
}

/** 助手头像：固定的书本/检索图形，与会话列表的品牌色同源。 */
export function AssistantAvatar({ size = 30 }: { size?: number }) {
  return (
    <span className="avatar avatar-assistant" style={{ width: size, height: size }}
          aria-hidden="true">
      <svg viewBox="0 0 24 24" width={size * 0.56} height={size * 0.56}
           fill="none" stroke="currentColor" strokeWidth="2"
           strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
        <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
      </svg>
    </span>
  )
}
