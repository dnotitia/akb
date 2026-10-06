import { Marked, type marked } from 'marked'

/** Keep schema tokenizers out of Marked's process-wide singleton. */
export function createMarkdownParser(): typeof marked {
  // Tiptap types this as the callable singleton, but uses its parser methods,
  // defaults and Lexer constructor, all of which a Marked instance provides.
  return new Marked() as unknown as typeof marked
}
