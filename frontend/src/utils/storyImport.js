/** Root-only text selection. Contents are never inspected by this helper. */
export function prepareChapters(selection) {
  const all = Array.from(selection)
  const files = all.filter(file => {
    const path = file.webkitRelativePath || file.name
    return path.split('/').length <= 2 && /\.txt$/i.test(file.name)
  }).sort((a, b) => {
    const left = a.webkitRelativePath || a.name
    const right = b.webkitRelativePath || b.name
    return left.localeCompare(right, 'en', { numeric: true }) || (left < right ? -1 : left > right ? 1 : 0)
  })
  const bytes = files.reduce((sum, file) => sum + file.size, 0)
  let error = null
  if (!files.length || files.length > 1000) error = 'file_count'
  else if (files.some(file => !file.size)) error = 'empty_file'
  else if (files.some(file => file.size > 2 * 1024 * 1024)) error = 'file_too_large'
  else if (bytes > 12 * 1024 * 1024) error = 'total_too_large'
  return { files, bytes, ignored: all.length - files.length, error }
}
