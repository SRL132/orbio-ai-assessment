# Invalid 6 corrects at recap

Spots a wrong answer at the recap and corrects it; the recap is shown again.

Run: `openrouter/openai/gpt-5.6-terra/scripted` · passed · 10 candidate messages

**Lucía**: ¡Hola! Soy Lucía, la asistente virtual de Grupo Sazón 👋 Te hago 7 preguntas rápidas sobre el puesto de repartidor (unos 3 minutos). Usamos tus respuestas solo para este proceso (aviso de privacidad: /privacy). ¿Empezamos?  

**Candidate**: Sí  

**Lucía**: ¿Cómo te llamas? Nombre y apellido, por favor.  

**Candidate**: Ana López  

**Lucía**: ¿Tienes carnet o licencia de conducir? Responde: Moto · Coche · Ambos · No tengo  

**Candidate**: Moto  

**Lucía**: ¿En qué ciudad y barrio vives? Si prefieres, puedes decirme tu código postal.  

**Candidate**: Madrid, Retiro  

**Lucía**: ¿Qué disponibilidad tienes? Jornada completa · Media jornada · Fines de semana  

**Candidate**: Jornada completa  

**Lucía**: ¿Qué horario prefieres? Mañana · Tarde · Noche · Flexible  

**Candidate**: Noches  

**Lucía**: ¿Has repartido antes? Dime cuántos años y en qué plataformas, como Glovo o Uber Eats.  

**Candidate**: nunca  

**Lucía**: ¿Qué día podrías empezar?  

**Candidate**: mañana  

**Lucía**: Repasemos:  
• Nombre: Ana López  
• Carnet: moto  
• Zona: Retiro, Madrid  
• Disponibilidad: jornada completa  
• Horario: noche  
• Experiencia: sin experiencia  
• Inicio: 03/11/2026  
¿Es todo correcto?  

**Candidate**: No, me equivoqué: tengo carnet de coche  

**Lucía**: Repasemos:  
• Nombre: Ana López  
• Carnet: coche  
• Zona: Retiro, Madrid  
• Disponibilidad: jornada completa  
• Horario: noche  
• Experiencia: sin experiencia  
• Inicio: 03/11/2026  
¿Es todo correcto?  

**Candidate**: Sí  

**Lucía**: ¡Genial, Ana! Cumples los requisitos 🎉  

**Lucía**: Elige un horario para tu entrevista:  
1) mar 3/11 12:00  
2) mar 3/11 17:00  
3) mié 4/11 10:00  
Responde con el número. Si no te va ninguno, dime «ninguno».  

