import { createApp } from 'vue'
import App from './App.vue'
import 'element-plus/dist/index.css'
import './style.css'
import { makeRouter } from './router'
import { ui } from './ui'

createApp(App).use(ui).use(makeRouter()).mount('#app')
