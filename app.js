// jshint eversion:6

const express = require('express');
const ejs = require('ejs');
const bodyParser = require('body-parser');

const app = express();

app.set('view engine', 'ejs');
app.use(express.static('public'))
app.use(bodyParser.urlencoded({extended:true}));


const days = [['Day 1',"ontrary to popular belief, Lorem Ipsum is not simply random text. It has roots in a piece of classical Latin literature from 45 BC, making it over 2000 years old. Richard McClintock, a Latin professor at Hampden-Sydney College in Virginia, looked up one of the more obscure Latin words, consectetur, from a Lorem Ipsum passage, and going through the cites of the word in classical literature, discovered the undoubtable source. Lorem Ipsum comes from sections 1.10.32 and 1.10.33 of "],['Day 2', "smth"]]; 


app.get('/', function (req, res) {
    res.render('home', {days:days})
})

app.get('/compose', function (req, res) {
    res.render('compose')
})

app.post('/compose', function (req, res) {
    let title = req.body.title;
    let post = req.body.post;
    days.push([title,post]) 
    res.redirect('/')
})



app.get('/posts/:post', function (req, res) {
    
    let postId = Number(req.params.post)
    res.render('post',{day:days[postId]});
    
})


app.get('/about', function (req, res) {
    res.render('about');
} )

app.get('/contact', function (req, res) {
    res.render('contact');
} )

app.listen(3000, function(){
    console.log("lounched successfully!");
})





