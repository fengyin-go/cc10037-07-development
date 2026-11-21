import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import SamplingQualityGate from '@/views/SamplingQualityGate.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/sampling-quality', name: 'sampling-quality', component: SamplingQualityGate },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
