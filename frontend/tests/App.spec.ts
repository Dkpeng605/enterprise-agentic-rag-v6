import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import App from '../src/App.vue'

describe('App', () => {
  it('renders the runnable Vue and TypeScript skeleton', () => {
    const wrapper = mount(App)

    expect(wrapper.get('h1').text()).toBe('Enterprise Agentic RAG v6')
    expect(wrapper.get('[data-testid="skeleton-status"]').text()).toContain('skeleton ready')
  })
})

