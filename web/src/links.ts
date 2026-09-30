export const REPO = "https://github.com/Vizanix/Atlas";
export const DOCS = `${REPO}/blob/main/docs`;
export function safeHttpUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}
