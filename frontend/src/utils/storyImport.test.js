import test from 'node:test'
import assert from 'node:assert/strict'
import { prepareChapters } from './storyImport.js'
const file = (path, size = 20) => ({ name: path.split('/').at(-1), webkitRelativePath: path, size })
test('root files only, natural order, duplicate chapter numbers retained', () => {
  const result = prepareChapters([file('root/10_End.txt'), file('root/2_B.txt'), file('root/2_A.txt'), file('root/sub/1.txt'), file('root/image.png')])
  assert.deepEqual(result.files.map(f => f.name), ['2_A.txt', '2_B.txt', '10_End.txt'])
  assert.equal(result.ignored, 2)
})
test('bounds and empty selection block confirmation', () => {
  assert.equal(prepareChapters([]).error, 'file_count')
  assert.equal(prepareChapters([file('a.txt', 0)]).error, 'empty_file')
  assert.equal(prepareChapters([file('a.txt', 2097153)]).error, 'file_too_large')
  assert.equal(prepareChapters(Array.from({ length: 1001 }, (_, i) => file(`${i}.txt`))).error, 'file_count')
})
