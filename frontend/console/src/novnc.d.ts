declare module '@novnc/novnc' {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: { shared?: boolean })
    scaleViewport: boolean
    resizeSession: boolean
    disconnect(): void
    focus(options?: FocusOptions): void
    clipboardPasteFrom(text: string): void
  }
}
