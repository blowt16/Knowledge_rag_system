/**
 * 把服务端返回的文件存到本地。
 *
 * ⚠️ **为什么不能直接 `<a href="/api/...">`**：那条路**带不上 `Authorization` 头**
 *    （`api/client.ts` 里 `requestBlob` 的注释已经写明这个坑）。
 *    所以先自己把 blob 取回来，再造一个临时 `<a download>` 触发下载。
 *
 * 项目里以前**没有**「点一下存到本地」的先例（`requestBlob` 只把 blob 喂给
 * PDF 阅读器），所以这个小工具是新写的 —— 放 `lib/` 下，日后要导出报告
 * 也是它（§11.3-5）。
 */

/** 触发浏览器下载一个已经在内存里的 blob。 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  // 有的浏览器要求节点在文档里才认 `click()`
  document.body.appendChild(a)
  a.click()
  // ⚠️ **撤 URL 与摘节点都放在下一个事件循环里**，不要紧挨着 `click()`。
  //    立刻 `revokeObjectURL` 会让部分浏览器拿到空文件（chunked 响应尤其明显）；
  //    而 `click()` 后立刻 `a.remove()` 会让下载**认不出是哪个元素发起的** ——
  //    实测 bsk 的下载拦截就抓不到，自动化里表现为「点了但什么也没发生」。
  //    这个顺序对用户和自动化都稳。
  setTimeout(() => {
    a.remove()
    URL.revokeObjectURL(url)
  }, 0)
}

/**
 * 从 `Content-Disposition` 里取服务端定的文件名。
 *
 * ⚠️ 文件名**由服务端消毒**（评测集名里可能有 `/ \ : * ? " < > |`），
 *    前端再拼一次就会和服务端的规则漂移 —— 所以能读头就读头。
 */
export function filenameFrom(header: string | null): string | null {
  if (!header) return null
  // RFC 5987：`filename*=UTF-8''%E5%94%AE%E5%90%8E.json`
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header)
  if (star) {
    try {
      return decodeURIComponent(star[1].trim())
    } catch {
      return star[1].trim()     // 编码坏了也别让下载炸掉
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header)
  return plain ? plain[1].trim() : null
}
