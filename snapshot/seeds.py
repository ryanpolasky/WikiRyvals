"""Seed articles for the BFS crawl.

Chosen to be high in-degree, well-connected, and instantly recognizable so the
bounded snapshot stays dense and the game feels like "real" Wikipedia rather
than a graph of stubs.
"""

SEEDS = [
    "World War II",
    "United States",
    "Albert Einstein",
    "Physics",
    "Mathematics",
    "Philosophy",
    "Music",
    "Film",
    "Napoleon",
    "Roman Empire",
    "Computer",
    "Internet",
    "Biology",
    "Chemistry",
    "Earth",
    "Sun",
    "Water",
    "Human",
    "London",
    "Japan",
    "India",
    "China",
    "France",
    "Football",
    "Chess",
    "Mountain",
    "Ocean",
    "Language",
    "History",
    "Art",
]

# Additive seeds for `python -m snapshot.build_snapshot --extend`. The base SEEDS
# above produce a concept/geography-heavy snapshot, so people and animals (which
# tend to be leaf nodes a bounded BFS never reaches) almost never become race
# endpoints. Crawling outward from these grafts those neighborhoods onto the
# existing snapshot WITHOUT rebuilding it. Trim/extend freely.
PEOPLE_SEEDS = [
    "Isaac Newton",
    "Charles Darwin",
    "Marie Curie",
    "Galileo Galilei",
    "Leonardo da Vinci",
    "William Shakespeare",
    "Ludwig van Beethoven",
    "Wolfgang Amadeus Mozart",
    "Aristotle",
    "Plato",
    "Socrates",
    "Nikola Tesla",
    "Thomas Edison",
    "Abraham Lincoln",
    "George Washington",
    "Winston Churchill",
    "Mahatma Gandhi",
    "Nelson Mandela",
    "Julius Caesar",
    "Alexander the Great",
    "Cleopatra",
    "Genghis Khan",
    "Confucius",
    "Vincent van Gogh",
    "Pablo Picasso",
    "Stephen Hawking",
    "Charles Dickens",
    "Elvis Presley",
    "Michael Jackson",
    "Queen Victoria",
]

ANIMAL_SEEDS = [
    "Dog",
    "Cat",
    "Lion",
    "Tiger",
    "Elephant",
    "Horse",
    "Dolphin",
    "Whale",
    "Shark",
    "Eagle",
    "Dinosaur",
    "Bird",
    "Fish",
    "Reptile",
    "Insect",
    "Spider",
    "Butterfly",
    "Snake",
    "Crocodile",
    "Bear",
    "Wolf",
    "Fox",
    "Rabbit",
    "Deer",
    "Chicken",
    "Frog",
    "Octopus",
    "Penguin",
    "Owl",
    "Kangaroo",
    "Giraffe",
    "Zebra",
    "Gorilla",
    "Chimpanzee",
    "Honey bee",
]

# Default seed set used by `--extend` when no explicit --seeds are given.
EXTRA_SEEDS = PEOPLE_SEEDS + ANIMAL_SEEDS
