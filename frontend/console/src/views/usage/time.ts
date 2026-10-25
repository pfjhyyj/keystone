// A single reporting clock: browser timezone settings do not change the statistics range.
const timezone = 'Asia/Shanghai'
export function shanghaiInput(value: string) {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return value
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-GB', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    })
      .formatToParts(date)
      .map((part) => [part.type, part.value]),
  )
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`
}
export const shanghaiDate = (value: string) => new Date(`${value}+08:00`)
export const statisticsTime = (value: string) =>
  new Date(value).toLocaleString('zh-CN', { timeZone: timezone, hour12: false })
export function statisticsRange(value: string, now = new Date()) {
  const until = new Date(now),
    day = shanghaiInput(until.toISOString()).slice(0, 10)
  const since =
    value === 'today'
      ? shanghaiDate(`${day}T00:00:00`)
      : value === 'month'
        ? shanghaiDate(`${day.slice(0, 7)}-01T00:00:00`)
        : new Date(until.getTime() - Number(value) * 86400000)
  return { since: since.toISOString(), until: until.toISOString() }
}
