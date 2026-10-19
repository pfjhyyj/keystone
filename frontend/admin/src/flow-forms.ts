// Application composition: remove this entry and import to retire the example form.
import type { Component } from 'vue'
import DocumentReviewInput from './businesses/document-review/FlowInput.vue'

export const flowForms: Readonly<Record<string, Component>> = {
  'document.review': DocumentReviewInput,
}
