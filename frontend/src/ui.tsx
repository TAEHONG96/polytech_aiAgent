export function ErrorNotice({ message }: { message: string }) {
  return message && <p className="notice" role="alert">{message}</p>
}
