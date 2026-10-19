import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import { makeRouter } from './router'

createApp(App).use(makeRouter()).mount('#app')
