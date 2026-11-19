/** Additional listeners use the same configured address as the primary endpoint. */
export function additionalDeviceEndpoints(primary: string, ports?: readonly number[]): string[] {
  const match = /^(.*):(\d+)$/.exec(primary)
  if (!match?.[1]) return []
  const primaryPort = Number(match[2])
  return [...new Set(ports ?? [])]
    .filter((port) => Number.isInteger(port) && port > 0 && port <= 65535 && port !== primaryPort)
    .map((port) => `${match[1]}:${port}`)
}
