/**
 * 极薄的 ECharts 包装。
 *
 * 为什么不用 `echarts-for-react`：只需要「挂载 → setOption → 卸载」这一件事，
 * 多一个依赖不值得（方案 §4.1 只点名了 ECharts 本身）。
 */
import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'

export default function EChart({
  option,
  height = 260,
}: {
  option: echarts.EChartsOption
  height?: number
}) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!box.current) return
    chart.current = echarts.init(box.current)
    const onResize = () => chart.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      // dispose 而不是 clear：容器会被 React 复用，留着实例会内存泄漏
      chart.current?.dispose()
      chart.current = null
    }
  }, [])

  useEffect(() => {
    chart.current?.setOption(option, true)
  }, [option])

  return <div ref={box} style={{ width: '100%', height }} />
}
